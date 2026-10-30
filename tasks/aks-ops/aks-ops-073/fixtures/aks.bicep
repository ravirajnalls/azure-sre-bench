param location string = resourceGroup().location
param clusterName string = 'orders-prod-aks'
param kubernetesVersion string = '1.30.7'

var clusterTags = {
  environment: 'prod'
  costcenter: '4471'
}

resource snetWorkload 'Microsoft.Network/virtualNetworks/subnets@2024-03-01' existing = {
  name: 'orders-vnet/snet-orders-workload'
}

resource cluster 'Microsoft.ContainerService/managedClusters@2024-03-02-preview' = {
  name: clusterName
  location: location
  tags: clusterTags
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName
  }
}

resource poolWorkload 'Microsoft.ContainerService/managedClusters/agentPools@2024-03-02-preview' = {
  parent: cluster
  name: 'npworkload'
  properties: {
    orchestratorVersion: kubernetesVersion
    count: 20
    maxPods: 30
    mode: 'User'
    vmSize: 'Standard_D4s_v5'
    networkPlugin: 'azure'
    vnetSubnetId: snetWorkload.id
    upgradeSettings: {
      maxSurge: '100%'
    }
  }
}
