// Microsoft Foundry resource (AIServices, project management enabled) + project +
// model deployments. The same AIServices account also serves Azure Speech
// (https://<account>.cognitiveservices.azure.com/), so no separate Speech resource.
param location string
param accountName string
param projectName string
param tags object

@description('Model deployments: { name, format, model, version, sku, capacity, providerData? }. format is OpenAI, Anthropic, DeepSeek, xAI, ...')
param modelDeployments array

@description('Image model deployment (same object shape). Empty name skips it.')
param imageDeployment object

@description('Attestation sent as modelProviderData for Anthropic deployments; accepts the Marketplace offer on your behalf.')
param anthropicProviderData object = {}

param workloadPrincipalId string
param deployerPrincipalId string = ''

var cognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var foundryUser = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var foundryProjectManager = 'eadc314b-1a2d-4efa-be10-5d325db5065e'

resource account 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    // Keyless only: Entra ID tokens for inference, agents and speech.
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Story studio prompt agents and hosted workflow'
  }
}

var allDeployments = concat(modelDeployments, empty(imageDeployment.name) ? [] : [imageDeployment])

// Deployments on one account must be created one at a time.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = [
  for item in allDeployments: {
    parent: account
    name: item.name
    sku: {
      name: item.sku
      capacity: item.capacity
    }
    properties: union(
      {
        model: {
          format: item.format
          name: item.model
          version: item.version
        }
        versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
        raiPolicyName: 'Microsoft.DefaultV2'
      },
      item.format == 'Anthropic' ? { modelProviderData: anthropicProviderData } : {}
    )
    dependsOn: [
      project
    ]
  }
]

// The workload identity calls models/agents (Foundry User on the project) and
// Speech/OpenAI data planes (Cognitive Services User on the account).
resource workloadCognitive 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, workloadPrincipalId, cognitiveServicesUser)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: workloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource workloadFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, workloadPrincipalId, foundryUser)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUser)
    principalId: workloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The CI/deployer principal publishes prompt-agent versions and the hosted agent,
// which requires Foundry Project Manager on the project.
resource deployerFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(project.id, deployerPrincipalId, foundryProjectManager)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManager)
    principalId: deployerPrincipalId
  }
}

output accountId string = account.id
output accountName string = account.name
output projectId string = project.id
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output speechEndpoint string = 'https://${account.name}.cognitiveservices.azure.com/'
output openAiEndpoint string = 'https://${account.name}.openai.azure.com/'
output deploymentNames array = [for item in allDeployments: item.name]
