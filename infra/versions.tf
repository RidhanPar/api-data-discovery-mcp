terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.81" }
    random  = { source = "hashicorp/random", version = "~> 3.9" }
    time    = { source = "hashicorp/time", version = "~> 0.14" }
  }
  # State holds generated secrets. For anything beyond a personal demo, use a remote
  # backend with encryption and access control, e.g.:
  # backend "azurerm" { resource_group_name = "tfstate" storage_account_name = "..." container_name = "tfstate" key = "nordlys.tfstate" use_azuread_auth = true }
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false # one-command teardown
    }
    key_vault {
      purge_soft_delete_on_destroy = true # so `make azure-up` works again after `make azure-down`
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true
    }
  }
  subscription_id = var.subscription_id
}
