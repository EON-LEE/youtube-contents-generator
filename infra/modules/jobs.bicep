// Container Apps environment (logs to Azure Monitor via diagnostic settings, so no
// workspace shared key is needed) and the render/upload/analytics job.
param location string
param namePrefix string
param tags object
param workspaceId string
param identityId string
param identityClientId string
param registryLoginServer string

@description('Full image reference. Before the first `az acr build`, a public placeholder lets the job deploy.')
param image string

@allowed(['Manual', 'Schedule'])
param triggerType string = 'Manual'
param cronExpression string = '0 22 * * *'
param replicaTimeoutSeconds int = 14400
param cpu string = '2'
param memory string = '4Gi'
param environmentVariables array

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: environment
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

var usesAcr = startsWith(image, registryLoginServer)

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: '${namePrefix}-job'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: triggerType
      replicaTimeout: replicaTimeoutSeconds
      replicaRetryLimit: 0
      manualTriggerConfig: triggerType == 'Manual'
        ? {
            parallelism: 1
            replicaCompletionCount: 1
          }
        : null
      scheduleTriggerConfig: triggerType == 'Schedule'
        ? {
            cronExpression: cronExpression
            parallelism: 1
            replicaCompletionCount: 1
          }
        : null
      registries: usesAcr
        ? [
            {
              server: registryLoginServer
              identity: identityId
            }
          ]
        : []
    }
    template: {
      containers: [
        {
          name: 'studio'
          image: image
          args: ['job']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat(
            [
              {
                name: 'AZURE_CLIENT_ID'
                value: identityClientId
              }
            ],
            environmentVariables
          )
        }
      ]
    }
  }
}

output environmentId string = environment.id
output jobId string = job.id
output jobName string = job.name
