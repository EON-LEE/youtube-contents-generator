// Storage for runs, the shared library, rendered outputs, durable studio state and the analytics table.
// Shared-key access is disabled: every client authenticates with Entra ID.
param location string
param storageName string
param tags object
param workloadPrincipalId string
param logicAppHostPrincipalId string = ''
@description('Extra principals (for example the Foundry hosted-agent identity) that read/write run blobs.')
param blobContributorPrincipalIds array = []

var blobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var tableDataContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var queueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 14
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in ['runs', 'library', 'outputs', 'state']: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource analyticsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'analytics'
}

var workloadRoles = [blobDataContributor, tableDataContributor]
// Logic Apps Standard keeps its runtime state in blobs, queues and tables.
var hostRoles = [blobDataContributor, queueDataContributor, tableDataContributor]

resource workloadAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in workloadRoles: {
    name: guid(storage.id, workloadPrincipalId, role)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
      principalId: workloadPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource hostAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in hostRoles: if (!empty(logicAppHostPrincipalId)) {
    name: guid(storage.id, logicAppHostPrincipalId, role, 'host')
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
      principalId: logicAppHostPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource extraBlobAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in blobContributorPrincipalIds: {
    name: guid(storage.id, principalId, blobDataContributor)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributor)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output storageId string = storage.id
output storageName string = storage.name
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output queueEndpoint string = storage.properties.primaryEndpoints.queue
output tableEndpoint string = storage.properties.primaryEndpoints.table
