// Container registry for the render job image and the hosted-agent image.
// Admin user is disabled; pulls use managed identities (AcrPull).
param location string
param registryName string
param tags object
param pullPrincipalIds array

var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource pullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in pullPrincipalIds: {
    name: guid(registry.id, principalId, acrPull)
    scope: registry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPull)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output registryId string = registry.id
output registryName string = registry.name
output loginServer string = registry.properties.loginServer
