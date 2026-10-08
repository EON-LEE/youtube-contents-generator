// Logic Apps Standard (single-tenant) orchestrator.
// Standard is used per the plan: stateful workflows with long Until/Delay polling,
// workflow files deployed from source control (infra/logicapp), and keyless host
// storage through a managed identity. It runs on a Workflow Standard (WS1) plan,
// which is a fixed monthly cost; see infra/README.md for the Consumption trade-off.
param location string
param namePrefix string
param tags object
param storageName string
param blobEndpoint string
param queueEndpoint string
param tableEndpoint string
param hostIdentityId string
param hostIdentityClientId string
param appInsightsConnectionString string
param actionGroupId string
param foundryAccountName string
param foundryProjectName string
param projectEndpoint string
param jobName string
param hostedAgentName string = 'story-studio'
param episodeHourUtc int
param learningHourUtc int

var jobsOperator = 'b9a307c4-5aa3-4b52-ba60-2b17c136cd7b'
var jobsReader = 'edd66693-d32a-450b-997d-0158c03976b0'
var foundryUser = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${namePrefix}-wf-plan'
  location: location
  tags: tags
  sku: {
    name: 'WS1'
    tier: 'WorkflowStandard'
  }
  kind: 'elastic'
  properties: {
    maximumElasticWorkerCount: 1
  }
}

resource logicApp 'Microsoft.Web/sites@2023-12-01' = {
  name: '${namePrefix}-orchestrator'
  location: location
  tags: tags
  kind: 'functionapp,workflowapp'
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${hostIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      netFrameworkVersion: 'v6.0'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      functionsRuntimeScaleMonitoringEnabled: true
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'dotnet' }
        { name: 'APP_KIND', value: 'workflowApp' }
        { name: 'AzureFunctionsJobHost__extensionBundle__id', value: 'Microsoft.Azure.Functions.ExtensionBundle.Workflows' }
        { name: 'AzureFunctionsJobHost__extensionBundle__version', value: '[1.*, 2.0.0)' }
        // Keyless host storage (user-assigned identity).
        { name: 'AzureWebJobsStorage__managedIdentityResourceId', value: hostIdentityId }
        { name: 'AzureWebJobsStorage__clientId', value: hostIdentityClientId }
        { name: 'AzureWebJobsStorage__credential', value: 'managedIdentity' }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: blobEndpoint }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: queueEndpoint }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: tableEndpoint }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        // Read by infra/logicapp/parameters.json via @appsetting().
        { name: 'STUDIO_PROJECT_ENDPOINT', value: projectEndpoint }
        { name: 'STUDIO_HOSTED_AGENT', value: hostedAgentName }
        { name: 'STUDIO_JOB_RESOURCE_ID', value: resourceId('Microsoft.App/jobs', jobName) }
        { name: 'STUDIO_EPISODE_HOUR_UTC', value: string(episodeHourUtc) }
        { name: 'STUDIO_LEARNING_HOUR_UTC', value: string(learningHourUtc) }
        { name: 'STUDIO_STORAGE_ACCOUNT', value: storageName }
      ]
    }
  }
}

resource job 'Microsoft.App/jobs@2024-03-01' existing = {
  name: jobName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

// Start the job and read its executions.
resource jobRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in [jobsOperator, jobsReader]: {
    name: guid(job.id, logicApp.id, role)
    scope: job
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
      principalId: logicApp.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Invoke the hosted agent endpoint of the project.
resource foundryUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, logicApp.id, foundryUser)
  scope: foundryProject
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUser)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Any failed run of either workflow (agent failure, job failure, timeout) emails the owner.
resource failedRunsAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-workflow-failed'
  location: 'global'
  tags: tags
  properties: {
    description: 'A studio orchestration workflow run failed.'
    severity: 2
    enabled: true
    scopes: [
      logicApp.id
    ]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'failed-runs'
          metricName: 'WorkflowRunsCompleted'
          metricNamespace: 'Microsoft.Web/sites'
          dimensions: [
            {
              name: 'status'
              operator: 'Include'
              values: ['Failed']
            }
          ]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroupId
      }
    ]
  }
}

output logicAppName string = logicApp.name
output logicAppPrincipalId string = logicApp.identity.principalId
