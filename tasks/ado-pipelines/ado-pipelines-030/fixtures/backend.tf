terraform {
  backend "azurerm" {
    resource_group_name  = "rg-prod-tfstate"
    storage_account_name = "stprodtfstate001"
    container_name       = "tfstate"
    key                  = "prod.terraform.tfstate"
    use_azuread_auth     = true
  }

  required_version = ">= 1.7.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
  }
}
