// Story studio: managed Azure infrastructure for the autonomous audio-drama pipeline.
//
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.parameters.json
//
// Everything authenticates with Microsoft Entra ID (managed identities); no keys or
// connection secrets are created or output.
targetScope = 'resourceGroup'

@description('Region for storage, registry, Key Vault, Container Apps, Logic Apps and monitoring.')
param location string = 'koreacentral'

@description('Region for the Foundry (AIServices) resource. Partner models (Claude, Grok, DeepSeek) are not offered in every region; eastus2 and swedencentral currently host the Claude families. Confirm availability for every model before deploying.')
param foundryLocation string = 'eastus2'

@minLength(3)
@maxLength(12)
@description('Lowercase prefix for resource names.')
param namePrefix string = 'storystudio'

param tags object = {
  workload: 'story-studio'
}

@description('Model deployments named as in studio.toml [agents]. Versions and capacities are PLACEHOLDERS to confirm against the live catalog (see infra/README.md).')
param modelDeployments array = [
  { name: 'gpt-5.5', format: 'OpenAI', model: 'gpt-5.5', version: '2026-04-24', sku: 'GlobalStandard', capacity: 50 }
  { name: 'gpt-5-mini', format: 'OpenAI', model: 'gpt-5-mini', version: '2025-08-07', sku: 'GlobalStandard', capacity: 100 }
  { name: 'claude-opus-5-5', format: 'Anthropic', model: 'claude-opus-5-5', version: '2', sku: 'GlobalStandard', capacity: 25 }
  { name: 'claude-sonnet-5', format: 'Anthropic', model: 'claude-sonnet-5', version: '2', sku: 'GlobalStandard', capacity: 25 }
  { name: 'deepseek-v3-2', format: 'DeepSeek', model: 'DeepSeek-V3.2', version: '1', sku: 'GlobalStandard', capacity: 50 }
  { name: 'grok-4-1', format: 'xAI', model: 'grok-4-1-fast-reasoning', version: '1', sku: 'GlobalStandard', capacity: 50 }
]

@description('Image model deployment (studio.toml foundry.image_deployment). Set name to empty string to skip.')
param imageDeployment object = {
  name: 'gpt-image-1'
  format: 'OpenAI'
  model: 'gpt-image-1'
  version: '2025-04-15'
  sku: 'GlobalStandard'
  capacity: 1
}

@description('Anthropic Marketplace attestation sent with Claude deployments: { organizationName, countryCode, industry }. Required when any deployment uses format Anthropic.')
param anthropicProviderData object = {}

@description('Monthly budget for the resource group in USD.')
param budgetAmountUsd int = 400

@description('Email for budget alerts and failed-workflow alerts.')
param alertEmail string

@description('First day of the current month (yyyy-MM-01) for the budget period.')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

@description('Set to private to upload approved episodes as private YouTube videos; empty disables uploads.')
@allowed([ 'private', '' ])
param uploadMode string = 'private'

@description('Job image. Leave empty to deploy a public placeholder before the first `az acr build`.')
param jobImage string = ''

@allowed(['Manual', 'Schedule'])
@description('Manual: the Logic App starts the job. Schedule: the job runs itself on jobCronExpression (UTC).')
param jobTriggerType string = 'Manual'
param jobCronExpression string = '0 22 * * *'

@description('Deploy the Logic Apps Standard orchestrator (WS1 plan, fixed monthly cost).')
param deployOrchestrator bool = true
@minValue(0)
@maxValue(23)
param episodeHourUtc int = 21
@minValue(0)
@maxValue(23)
param learningHourUtc int = 3

@description('Principal (user or CI service principal) that publishes agents; receives Foundry Project Manager.')
param deployerPrincipalId string = ''

@description('Entra object id of the Foundry hosted agent identity (known after its first deployment); receives blob access for run state.')
param hostedAgentPrincipalId string = ''

var suffix = uniqueString(resourceGroup().id)
var compactPrefix = replace(namePrefix, '-', '')
var placeholderImage = 'mcr.microsoft.com/k8se/quickstart-jobs:latest'

resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-workload-id'
  location: location
  tags: tags
}

resource orchestratorHostIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-orchestrator-host-id'
  location: location
  tags: tags
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    alertEmail: alertEmail
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageName: take('${compactPrefix}${suffix}', 24)
    tags: tags
    workloadPrincipalId: workloadIdentity.properties.principalId
    logicAppHostPrincipalId: deployOrchestrator ? orchestratorHostIdentity.properties.principalId : ''
    blobContributorPrincipalIds: empty(hostedAgentPrincipalId) ? [] : [hostedAgentPrincipalId]
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    keyVaultName: take('${compactPrefix}kv${suffix}', 24)
    tags: tags
    workloadPrincipalId: workloadIdentity.properties.principalId
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: foundryLocation
    accountName: '${namePrefix}-ai-${suffix}'
    projectName: '${namePrefix}-project'
    tags: tags
    modelDeployments: modelDeployments
    imageDeployment: imageDeployment
    anthropicProviderData: anthropicProviderData
    workloadPrincipalId: workloadIdentity.properties.principalId
    deployerPrincipalId: deployerPrincipalId
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    registryName: '${compactPrefix}acr${suffix}'
    tags: tags
    // The project identity pulls the hosted-agent image.
    pullPrincipalIds: [
      workloadIdentity.properties.principalId
      foundry.outputs.projectPrincipalId
    ]
  }
}

module jobs 'modules/jobs.bicep' = {
  name: 'jobs'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    workspaceId: monitoring.outputs.workspaceId
    identityId: workloadIdentity.id
    identityClientId: workloadIdentity.properties.clientId
    registryLoginServer: registry.outputs.loginServer
    image: empty(jobImage) ? placeholderImage : jobImage
    triggerType: jobTriggerType
    cronExpression: jobCronExpression
    environmentVariables: [
      { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundry.outputs.projectEndpoint }
      { name: 'FOUNDRY_IMAGE_DEPLOYMENT', value: imageDeployment.name }
      { name: 'SPEECH_ENDPOINT', value: foundry.outputs.speechEndpoint }
      { name: 'SPEECH_REGION', value: foundryLocation }
      { name: 'STUDIO_STORAGE_ACCOUNT_URL', value: storage.outputs.blobEndpoint }
      { name: 'STUDIO_TABLE_ENDPOINT', value: storage.outputs.tableEndpoint }
      { name: 'KEY_VAULT_URL', value: keyVault.outputs.keyVaultUri }
      { name: 'STUDIO_UPLOAD', value: uploadMode }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
    ]
  }
}

module orchestrator 'modules/logicapp.bicep' = if (deployOrchestrator) {
  name: 'orchestrator'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    storageName: storage.outputs.storageName
    blobEndpoint: storage.outputs.blobEndpoint
    queueEndpoint: storage.outputs.queueEndpoint
    tableEndpoint: storage.outputs.tableEndpoint
    hostIdentityId: orchestratorHostIdentity.id
    hostIdentityClientId: orchestratorHostIdentity.properties.clientId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    actionGroupId: monitoring.outputs.actionGroupId
    foundryAccountName: foundry.outputs.accountName
    foundryProjectName: foundry.outputs.projectName
    projectEndpoint: foundry.outputs.projectEndpoint
    jobName: jobs.outputs.jobName
    episodeHourUtc: episodeHourUtc
    learningHourUtc: learningHourUtc
  }
}

module budget 'modules/budget.bicep' = {
  name: 'budget'
  params: {
    budgetName: '${namePrefix}-monthly'
    amountUsd: budgetAmountUsd
    alertEmail: alertEmail
    startDate: budgetStartDate
  }
}

output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_IMAGE_DEPLOYMENT string = imageDeployment.name
output FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output FOUNDRY_PROJECT_NAME string = foundry.outputs.projectName
output SPEECH_ENDPOINT string = foundry.outputs.speechEndpoint
output SPEECH_REGION string = foundryLocation
output STUDIO_STORAGE_ACCOUNT_URL string = storage.outputs.blobEndpoint
output STUDIO_TABLE_ENDPOINT string = storage.outputs.tableEndpoint
output KEY_VAULT_URL string = keyVault.outputs.keyVaultUri
output ACR_NAME string = registry.outputs.registryName
output ACR_LOGIN_SERVER string = registry.outputs.loginServer
output CONTAINER_APPS_JOB_NAME string = jobs.outputs.jobName
output LOGIC_APP_NAME string = orchestrator.?outputs.logicAppName ?? ''
output AZURE_CLIENT_ID string = workloadIdentity.properties.clientId
output MODEL_DEPLOYMENTS array = foundry.outputs.deploymentNames
