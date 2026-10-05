# Nordlys discovery platform on Azure: the smallest footprint that runs the full stack.
#
#   Container Apps (Consumption): catalog, mcp, agent, n8n, keycloak, mailpit + ingest job
#   PostgreSQL Flexible Server B1ms (pgvector) in a private subnet: catalog, n8n, keycloak DBs
#   Azure OpenAI (chat + embeddings), reached with managed identity only (keys disabled)
#   Key Vault (RBAC) for every secret; one managed identity per app, scoped per secret
#   Log Analytics, budget alert on the resource group
#
# Everything lives in one resource group, so `make azure-down` removes all of it.

data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  name   = var.prefix
  suffix = random_string.suffix.result
  tags   = { project = "nordlys-discovery", managed_by = "terraform" }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}-discovery"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.name}-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 1 # cost guard: a log storm cannot run up the bill
  tags                = local.tags
}

# ------------------------------------------------------------------ network

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.40.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "apps" {
  name                 = "snet-apps"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.0.0/23"]
  delegation {
    name = "aca"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "postgres" {
  name                 = "snet-postgres"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.2.0/28"]
  delegation {
    name = "pg"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "${local.name}.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "pg-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

# ------------------------------------------------------------------ secrets

resource "random_password" "secret" {
  for_each = toset(local.random_secrets)
  length   = 32
  special  = false # used inside URLs and shell-free env vars
}

locals {
  # Static names, so for_each keys are known at plan time on the first apply.
  random_secrets = [
    "db-password",
    "n8n-encryption-key",
    "webhook-secret",
    "link-signing-secret",
    "mcp-client-secret",
    "agent-client-secret",
    "n8n-client-secret",
    "registration-api-key",
    "keycloak-admin-password",
    "demo-password-alice",
    "demo-password-bob",
    "demo-password-dpo",
  ]
  pg_admin = "nordlysadmin"
  pg_host  = azurerm_postgresql_flexible_server.main.fqdn
  secrets = merge(
    { for k, v in random_password.secret : k => v.result },
    {
      "database-url" = "postgresql+psycopg://${local.pg_admin}:${random_password.secret["db-password"].result}@${local.pg_host}:5432/nordlys?sslmode=require"
    },
  )
}

resource "azurerm_key_vault" "main" {
  name                       = "kv-${local.name}-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false # demo environment: allows a clean teardown
  soft_delete_retention_days = 7
  tags                       = local.tags
}

# The identity running Terraform writes the secrets.
resource "azurerm_role_assignment" "deployer_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# RBAC assignments take a little while to propagate.
resource "time_sleep" "rbac" {
  create_duration = "60s"
  depends_on      = [azurerm_role_assignment.deployer_secrets, azurerm_role_assignment.app_secret]
}

resource "azurerm_key_vault_secret" "main" {
  for_each     = toset(concat(local.random_secrets, ["database-url"]))
  name         = each.key
  value        = local.secrets[each.key]
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.deployer_secrets]
  lifecycle {
    ignore_changes = [value] # rotate in Key Vault without Terraform fighting it
  }
}

# ------------------------------------------------------------------ registry and storage

resource "azurerm_container_registry" "main" {
  name                = "acr${local.name}${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
  admin_enabled       = false # pulls use managed identities
  tags                = local.tags
}

# Azure Files share for APIs published through Workflow B (catalog + ingest job).
resource "azurerm_storage_account" "main" {
  name                            = "st${local.name}${local.suffix}"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_share" "registrations" {
  name               = "registrations"
  storage_account_id = azurerm_storage_account.main.id
  quota              = 1
}

# ------------------------------------------------------------------ PostgreSQL

resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "pg-${local.name}-${local.suffix}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "17"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  backup_retention_days         = 7
  administrator_login           = local.pg_admin
  administrator_password        = random_password.secret["db-password"].result
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  zone                          = "1"
  tags                          = local.tags
  depends_on                    = [azurerm_private_dns_zone_virtual_network_link.postgres]
}

resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  for_each  = toset(["nordlys", "n8n", "keycloak"])
  name      = each.key
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# ------------------------------------------------------------------ Azure OpenAI

resource "azurerm_cognitive_account" "openai" {
  name                  = "oai-${local.name}-${local.suffix}"
  location              = azurerm_resource_group.main.location
  resource_group_name   = azurerm_resource_group.main.name
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "oai-${local.name}-${local.suffix}" # required for Entra ID auth
  local_auth_enabled    = false                               # no API keys: managed identity only
  tags                  = local.tags
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.chat_model.name
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.chat_model.name
    version = var.chat_model.version
  }
  sku {
    name     = var.chat_model.sku
    capacity = var.chat_model.capacity
  }
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.embedding_model.name
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.embedding_model.name
    version = var.embedding_model.version
  }
  sku {
    name     = var.embedding_model.sku
    capacity = var.embedding_model.capacity
  }
  depends_on = [azurerm_cognitive_deployment.chat] # the service rejects parallel deployments
}

# ------------------------------------------------------------------ budget

resource "azurerm_consumption_budget_resource_group" "main" {
  name              = "budget-${local.name}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.budget_amount
  time_grain        = "Monthly"
  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }
  dynamic "notification" {
    for_each = { "actual-50" = [50, "Actual"], "actual-80" = [80, "Actual"], "actual-100" = [100, "Actual"], "forecast-100" = [100, "Forecasted"] }
    content {
      enabled        = true
      threshold      = notification.value[0]
      threshold_type = notification.value[1]
      operator       = "GreaterThanOrEqualTo"
      contact_emails = var.budget_emails
    }
  }
  lifecycle {
    ignore_changes = [time_period] # set once, at creation
  }
}
