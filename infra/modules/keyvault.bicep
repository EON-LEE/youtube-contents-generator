// Key Vault in RBAC mode (no access policies) for third-party secrets such as the
// YouTube OAuth refresh token. Azure services themselves use managed identities.
param location string
param keyVaultName string
param tags object
param workloadPrincipalId string

var secretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource secretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, workloadPrincipalId, secretsUser)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUser)
    principalId: workloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output keyVaultId string = vault.id
output keyVaultUri string = vault.properties.vaultUri
