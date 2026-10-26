terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id

  # We register the three resource providers by hand (see DEPLOY.md) so
  # Terraform doesn't try to register ones a student subscription can't.
  resource_provider_registrations = "none"
}
