terraform {
  required_version = ">= 1.7.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
  }
}

# Renamed from aks_subnets to match the team naming standard.
variable "cluster_subnets" {
  type = map(object({
    prefix = string
  }))
  default = {
    aks = {
      prefix = "10.0.64.0/24"
    }
  }
}

variable "firewall_private_ip" {
  type    = string
  default = "10.0.100.4"
}

data "azurerm_subnet" "aks" {
  name                 = "snet-payments-aks"
  virtual_network_name = "payments-vnet"
  resource_group_name  = "rg-payments-01"
}

resource "azurerm_route_table" "egress_lockdown" {
  name                          = "rt-payments-egress-lockdown"
  location                      = "westeurope"
  resource_group_name           = "rg-payments-01"
  disable_bgp_route_propagation = false

  route {
    name                   = "default-via-firewall"
    address_prefix         = "0.0.0.0/0"
    next_hop_type          = "VirtualAppliance"
    next_hop_in_ip_address = var.firewall_private_ip
  }
}

resource "azurerm_subnet_route_table_association" "aks" {
  subnet_id      = data.azurerm_subnet.aks.id
  route_table_id = azurerm_route_table.egress_lockdown.id
}
