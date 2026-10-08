<#
.SYNOPSIS
  Deploys the story-studio Azure infrastructure (what-if first) and prints the
  environment variables the studio CLI, job and hosted agent need.

.EXAMPLE
  ./infra/deploy.ps1 -ResourceGroup rg-story-studio -Location koreacentral
  ./infra/deploy.ps1 -ResourceGroup rg-story-studio -BuildImages -DeployWorkflows -DeployHostedAgent

.NOTES
  Requires Azure CLI signed in (`az login`) with Owner or User Access Administrator +
  Contributor on the resource group (the templates create role assignments).
  Uses keyless auth only. Nothing is deployed until you confirm the what-if output
  (or pass -Yes).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $Location = 'koreacentral',
    [string] $ParametersFile = (Join-Path $PSScriptRoot 'main.parameters.json'),
    [string] $EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.env.azure'),
    [switch] $Yes,
    [switch] $BuildImages,
    [switch] $DeployWorkflows,
    [switch] $DeployHostedAgent,
    [string] $ImageTag = (Get-Date -Format 'yyyyMMddHHmmss')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path $PSScriptRoot -Parent
$template = Join-Path $PSScriptRoot 'main.bicep'
$deploymentName = "story-studio-$((Get-Date).ToString('yyyyMMddHHmmss'))"

function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments)] [string[]] $Arguments)
    & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "az $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path $ParametersFile)) {
    throw "Parameters file not found: $ParametersFile. Copy infra/main.parameters.example.json and fill in alertEmail, anthropicProviderData and model versions."
}

$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account) { throw 'Azure CLI is not signed in. Run az login first.' }
Write-Host "Subscription: $($account.name) ($($account.id))"

if ((az group exists --name $ResourceGroup) -ne 'true') {
    Write-Host "Creating resource group $ResourceGroup in $Location"
    Invoke-Az group create --name $ResourceGroup --location $Location --output none
}

$extraParameters = @()
function Deploy([string[]] $Extra) {
    Write-Host "`n== what-if ==" -ForegroundColor Cyan
    Invoke-Az deployment group what-if --resource-group $ResourceGroup --name $deploymentName `
        --template-file $template --parameters "@$ParametersFile" @Extra
    if (-not $Yes) {
        $answer = Read-Host 'Apply these changes? [y/N]'
        if ($answer -notin @('y', 'Y', 'yes')) { throw 'Deployment cancelled.' }
    }
    Write-Host "`n== deploy ==" -ForegroundColor Cyan
    $json = az deployment group create --resource-group $ResourceGroup --name $deploymentName `
        --template-file $template --parameters "@$ParametersFile" @Extra `
        --query properties.outputs --output json
    if ($LASTEXITCODE -ne 0) { throw 'Deployment failed.' }
    return ($json | ConvertFrom-Json)
}

$outputs = Deploy $extraParameters

if ($BuildImages) {
    $acr = $outputs.ACR_NAME.value
    $jobImage = "$($outputs.ACR_LOGIN_SERVER.value)/story-studio-job:$ImageTag"
    Write-Host "`n== building $jobImage and story-studio-agent:$ImageTag in ACR ==" -ForegroundColor Cyan
    Invoke-Az acr build --registry $acr --image "story-studio-job:$ImageTag" --file (Join-Path $repoRoot 'Dockerfile') $repoRoot
    Invoke-Az acr build --registry $acr --image "story-studio-agent:$ImageTag" `
        --file (Join-Path $PSScriptRoot 'hosted-agent/Dockerfile') $repoRoot
    $outputs = Deploy @('--parameters', "jobImage=$jobImage")
}

if ($DeployWorkflows -and $outputs.LOGIC_APP_NAME.value) {
    $zip = Join-Path $repoRoot '.story-pipeline/logicapp.zip'
    New-Item -ItemType Directory -Force (Split-Path $zip) | Out-Null
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path (Join-Path $PSScriptRoot 'logicapp/*') -DestinationPath $zip
    Write-Host "`n== deploying workflows to $($outputs.LOGIC_APP_NAME.value) ==" -ForegroundColor Cyan
    Invoke-Az logicapp deployment source config-zip --resource-group $ResourceGroup `
        --name $outputs.LOGIC_APP_NAME.value --src $zip
}

if ($DeployHostedAgent) {
    $agentImage = "$($outputs.ACR_LOGIN_SERVER.value)/story-studio-agent:$ImageTag"
    $env:FOUNDRY_PROJECT_ENDPOINT = $outputs.FOUNDRY_PROJECT_ENDPOINT.value
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    $script = @"
import json, os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from story_pipeline.studio.hosted import deploy_hosted_agent
project = AIProjectClient(endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=DefaultAzureCredential())
version = deploy_hosted_agent(project, '$agentImage', environment={
    'STUDIO_STORAGE_ACCOUNT_URL': '$($outputs.STUDIO_STORAGE_ACCOUNT_URL.value)',
    'STUDIO_RUNS_CONTAINER': 'runs'})
print(json.dumps({'name': version.name, 'version': version.version, 'status': getattr(version, 'status', None)}))
"@
    Write-Host "`n== creating hosted agent version from $agentImage ==" -ForegroundColor Cyan
    $script | python -
    if ($LASTEXITCODE -ne 0) { throw 'Hosted agent deployment failed.' }
    Write-Host 'Grant the hosted agent identity blob access: redeploy with hostedAgentPrincipalId=<agent identity object id>.'
}

$names = 'FOUNDRY_PROJECT_ENDPOINT', 'FOUNDRY_IMAGE_DEPLOYMENT', 'SPEECH_ENDPOINT', 'SPEECH_REGION',
    'STUDIO_STORAGE_ACCOUNT_URL', 'STUDIO_TABLE_ENDPOINT', 'KEY_VAULT_URL', 'ACR_LOGIN_SERVER',
    'CONTAINER_APPS_JOB_NAME', 'LOGIC_APP_NAME'
$lines = foreach ($name in $names) {
    $property = $outputs.PSObject.Properties[$name]
    if ($property -and $property.Value.value) { "$name=$($property.Value.value)" }
}
Set-Content -Path $EnvFile -Value $lines -Encoding utf8
Write-Host "`n== environment (also written to $EnvFile; contains no secrets) ==" -ForegroundColor Green
$lines | ForEach-Object { $k, $v = $_ -split '=', 2; "`$env:$k = '$v'" }
Write-Host "`nNext: python -m story_pipeline studio check-models; python -m story_pipeline studio deploy-agents"

