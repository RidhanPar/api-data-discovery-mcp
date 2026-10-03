# Offline plan test with mocked providers: `make azure-test` (no Azure credentials needed).
# Checks the security and cost properties of the configuration, not Azure itself.

mock_provider "azurerm" {
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      object_id       = "00000000-0000-0000-0000-000000000002"
      subscription_id = "00000000-0000-0000-0000-000000000003"
    }
  }
}
mock_provider "random" {}
mock_provider "time" {}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000003"
  budget_emails   = ["owner@example.com"]
  admin_cidrs     = ["203.0.113.10/32"]
}

run "plan" {
  command = plan

  assert {
    condition     = azurerm_cognitive_account.openai.local_auth_enabled == false
    error_message = "Azure OpenAI must not allow API keys"
  }
  assert {
    condition     = azurerm_key_vault.main.rbac_authorization_enabled && azurerm_container_registry.main.admin_enabled == false
    error_message = "Key Vault uses RBAC; ACR admin user is off"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.main.public_network_access_enabled == false
    error_message = "PostgreSQL must be private"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.main.sku_name == "B_Standard_B1ms"
    error_message = "cheapest PostgreSQL tier"
  }
  assert {
    condition     = length(azurerm_container_app.app["n8n"].ingress[0].ip_security_restriction) == 2
    error_message = "n8n is reachable only from admin CIDRs and the app subnet"
  }
  assert {
    condition     = alltrue([for k in ["catalog", "agent", "mailpit"] : azurerm_container_app.app[k].ingress[0].external_enabled == false])
    error_message = "catalog, agent and mailpit are internal"
  }
  assert {
    condition     = azurerm_container_app.app["n8n"].template[0].min_replicas == 1 && azurerm_container_app.app["catalog"].template[0].min_replicas == 0
    error_message = "only stateful/timer apps stay warm"
  }
  # Least privilege: each identity reads only its own secrets.
  assert {
    condition     = !contains(keys(local.secret_grants), "mcp/database-url") && !contains(keys(local.secret_grants), "agent/db-password")
    error_message = "mcp/agent must not read database credentials"
  }
  assert {
    condition     = contains(keys(local.secret_grants), "n8n/link-signing-secret") && !contains(keys(local.secret_grants), "catalog/link-signing-secret")
    error_message = "only n8n signs approval links"
  }
  assert {
    condition     = toset(keys(azurerm_role_assignment.openai_user)) == toset(["agent", "catalog", "ingest"])
    error_message = "only the agent, catalog and ingest job may call Azure OpenAI"
  }
  assert {
    condition     = length(azurerm_consumption_budget_resource_group.main.notification) == 4
    error_message = "budget alerts at 50/80/100% actual and 100% forecast"
  }
}

run "local_embeddings_need_no_openai_for_ingest" {
  command = plan
  variables {
    embedding_provider = "onnx-local"
  }
  assert {
    condition     = toset(keys(azurerm_role_assignment.openai_user)) == toset(["agent"])
    error_message = "with local embeddings only the agent uses Azure OpenAI"
  }
}
