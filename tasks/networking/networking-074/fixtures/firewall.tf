# Hub firewall: egress from the payments hub flows through this firewall.
# Internal tooling domains are allow-listed below; all other traffic is
# dropped by the firewall's default deny.
resource "azurerm_public_ip" "fw" {
  name                = "pip-af-payments-egress"
  location            = "westeurope"
  resource_group_name = "rg-payments-01"
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_firewall" "hub" {
  name                = "af-payments-egress"
  location            = "westeurope"
  resource_group_name = "rg-payments-01"
  sku_name            = "AZFW_VNet"
  sku_tier            = "Standard"
  ip_configuration {
    name                 = "fw-ip-config"
    subnet_id            = azurerm_subnet.firewall.id
    public_ip_address_id = azurerm_public_ip.fw.id
  }
}

resource "azurerm_subnet" "firewall" {
  name                 = "AzureFirewallSubnet"
  resource_group_name  = "rg-payments-01"
  virtual_network_name = "payments-vnet"
  address_prefixes     = ["10.0.100.0/26"]
}

resource "azurerm_firewall_application_rule_collection" "internal_tools" {
  name            = "arc-internal-tools"
  azure_firewall_name = azurerm_firewall.hub.name
  resource_group_name = "rg-payments-01"
  priority            = 100
  action              = "Allow"

  rule {
    name = "internal-tooling"
    source_addresses = ["10.0.0.0/16"]
    fqdn_tags        = []
    target_fqdns = [
      "time.windows.com",
      "*.internal.contoso.com"
    ]
    protocols {
      port = "443"
      type = "Https"
    }
  }
}

resource "azurerm_firewall_network_rule_collection" "corp_ranges" {
  name                = "nrc-corp-ranges"
  azure_firewall_name = azurerm_firewall.hub.name
  resource_group_name = "rg-payments-01"
  priority            = 110
  action              = "Allow"

  rule {
    name                  = "corp-https"
    protocols             = ["TCP"]
    source_addresses      = ["10.0.0.0/16"]
    destination_addresses = ["10.1.0.0/16"]
    destination_ports     = ["443"]
  }
}
