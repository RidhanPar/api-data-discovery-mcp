# Container Apps: the same images and settings as docker-compose.yml, with secrets from
# Key Vault and one managed identity per app.

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.name}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.apps.id
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
  tags = local.tags
}

resource "azurerm_container_app_environment_storage" "registrations" {
  name                         = "registrations"
  container_app_environment_id = azurerm_container_app_environment.main.id
  account_name                 = azurerm_storage_account.main.name
  share_name                   = azurerm_storage_share.registrations.name
  access_key                   = azurerm_storage_account.main.primary_access_key # Azure Files SMB needs the key
  access_mode                  = "ReadWrite"
}

locals {
  domain       = azurerm_container_app_environment.main.default_domain
  acr          = azurerm_container_registry.main.login_server
  app_image    = "${local.acr}/nordlys-discovery:${var.image_tag}"
  issuer       = "https://keycloak.${local.domain}/realms/nordlys"
  openai_url   = azurerm_cognitive_account.openai.endpoint
  admin_ranges = concat(var.admin_cidrs, azurerm_subnet.apps.address_prefixes) # + app-to-app traffic

  # Settings shared by the Python services (see src/nordlys_discovery/config.py).
  app_env = {
    NORDLYS_AUTH_ENABLED                      = "true"
    NORDLYS_OIDC_ISSUER                       = local.issuer
    NORDLYS_LOG_LEVEL                         = "INFO"
    NORDLYS_EMBEDDING_PROVIDER                = var.embedding_provider
    NORDLYS_AZURE_OPENAI_ENDPOINT             = local.openai_url
    NORDLYS_AZURE_OPENAI_EMBEDDING_DEPLOYMENT = azurerm_cognitive_deployment.embedding.name
    NORDLYS_AZURE_OPENAI_CHAT_DEPLOYMENT      = azurerm_cognitive_deployment.chat.name
  }

  apps = {
    catalog = {
      image         = local.app_image, port = 8000, external = false, transport = "http", restricted = false
      cpu           = 0.5, memory = "1Gi", min = 0, max = 2, probe = "/health/ready", command = null
      registrations = true, openai = var.embedding_provider == "azure-openai"
      env = merge(local.app_env, {
        NORDLYS_REGISTRATIONS_DIR          = "/data/registrations"
        NORDLYS_ACCESS_REQUEST_WEBHOOK_URL = "http://n8n/webhook/access-request-created"
      })
      secrets = { NORDLYS_DATABASE_URL = "database-url", NORDLYS_ACCESS_REQUEST_WEBHOOK_SECRET = "webhook-secret" }
    }
    mcp = {
      image   = local.app_image, port = 8001, external = true, transport = "http", restricted = false
      cpu     = 0.25, memory = "0.5Gi", min = 0, max = 3, probe = "/health/ready", registrations = false, openai = false
      command = ["python", "-m", "nordlys_discovery.mcp_server", "--host", "0.0.0.0", "--port", "8001"]
      env = merge(local.app_env, {
        NORDLYS_CATALOG_API_URL = "http://catalog"
        NORDLYS_MCP_PUBLIC_URL  = "https://mcp.${local.domain}/mcp"
      })
      secrets = { NORDLYS_MCP_CLIENT_SECRET = "mcp-client-secret" }
    }
    agent = {
      image   = local.app_image, port = 8002, external = false, transport = "http", restricted = false
      cpu     = 0.25, memory = "0.5Gi", min = 0, max = 2, probe = "/health/ready", registrations = false, openai = true
      command = ["uvicorn", "nordlys_discovery.agent.app:app", "--host", "0.0.0.0", "--port", "8002"]
      env = merge(local.app_env, {
        NORDLYS_LLM_PROVIDER    = "azure-openai"
        NORDLYS_CATALOG_API_URL = "http://catalog"
        NORDLYS_AGENT_MCP_URL   = "http://mcp/mcp"
      })
      secrets = { NORDLYS_AGENT_CLIENT_SECRET = "agent-client-secret" }
    }
    keycloak = {
      image      = "${local.acr}/nordlys-keycloak:${var.image_tag}", port = 8080, external = true, transport = "http"
      restricted = false # the token endpoint must be reachable by MCP clients
      cpu        = 0.5, memory = "1Gi", min = 0, max = 1, probe = null, registrations = false, openai = false
      command    = ["/opt/keycloak/bin/kc.sh", "start", "--import-realm"]
      env = {
        KC_DB                       = "postgres"
        KC_DB_URL                   = "jdbc:postgresql://${local.pg_host}:5432/keycloak?sslmode=require"
        KC_DB_USERNAME              = local.pg_admin
        KC_HOSTNAME                 = "https://keycloak.${local.domain}"
        KC_PROXY_HEADERS            = "xforwarded"
        KC_HTTP_ENABLED             = "true"
        KC_HEALTH_ENABLED           = "true"
        KC_BOOTSTRAP_ADMIN_USERNAME = "admin"
      }
      secrets = {
        KC_DB_PASSWORD              = "db-password"
        KC_BOOTSTRAP_ADMIN_PASSWORD = "keycloak-admin-password"
        # Read by the realm import (placeholders in deploy/keycloak/nordlys-realm.json).
        MCP_CLIENT_SECRET   = "mcp-client-secret"
        AGENT_CLIENT_SECRET = "agent-client-secret"
        N8N_CLIENT_SECRET   = "n8n-client-secret"
        DEMO_PASSWORD_ALICE = "demo-password-alice"
        DEMO_PASSWORD_BOB   = "demo-password-bob"
        DEMO_PASSWORD_DPO   = "demo-password-dpo"
      }
    }
    n8n = {
      image      = "${local.acr}/nordlys-n8n:${var.image_tag}", port = 5678, external = true, transport = "http"
      restricted = true # editor and approval links: admin/corporate networks only
      cpu        = 0.5, memory = "1Gi", min = 1, max = 1, probe = "/healthz", registrations = false, openai = false
      command    = null # wait nodes (approval timeouts) need a running process: no scale to zero
      env = {
        DB_TYPE                           = "postgresdb"
        DB_POSTGRESDB_HOST                = local.pg_host
        DB_POSTGRESDB_DATABASE            = "n8n"
        DB_POSTGRESDB_USER                = local.pg_admin
        DB_POSTGRESDB_SSL_ENABLED         = "true"
        N8N_LISTEN_ADDRESS                = "0.0.0.0"
        N8N_DIAGNOSTICS_ENABLED           = "false"
        N8N_PERSONALIZATION_ENABLED       = "false"
        N8N_TEMPLATES_ENABLED             = "false"
        N8N_VERSION_NOTIFICATIONS_ENABLED = "false"
        WEBHOOK_URL                       = "https://n8n.${local.domain}/"
        GENERIC_TIMEZONE                  = "Europe/Stockholm"
        N8N_BLOCK_ENV_ACCESS_IN_NODE      = "false"
        NODE_FUNCTION_ALLOW_BUILTIN       = "crypto"
        NORDLYS_CATALOG_URL               = "http://catalog"
        NORDLYS_TOKEN_URL                 = "${local.issuer}/protocol/openid-connect/token"
        NORDLYS_KEYCLOAK_ADMIN_URL        = "https://keycloak.${local.domain}/admin/realms/nordlys"
        NORDLYS_N8N_CLIENT_ID             = "nordlys-n8n"
        NORDLYS_APPROVAL_TIMEOUT_MINUTES  = tostring(var.approval_timeout_minutes)
        NORDLYS_OPS_EMAIL                 = "platform-ops@nordlys.example"
        NORDLYS_REVIEWER_EMAIL            = "api-governance@nordlys.example"
        NORDLYS_REVIEW_TIMEOUT_MINUTES    = "4320"
        NORDLYS_AGENT_URL                 = "http://agent"
      }
      secrets = {
        DB_POSTGRESDB_PASSWORD       = "db-password"
        N8N_ENCRYPTION_KEY           = "n8n-encryption-key"
        NORDLYS_N8N_CLIENT_SECRET    = "n8n-client-secret"
        NORDLYS_WEBHOOK_SECRET       = "webhook-secret"
        NORDLYS_LINK_SIGNING_SECRET  = "link-signing-secret"
        NORDLYS_REGISTRATION_API_KEY = "registration-api-key"
      }
    }
    # Demo mail sink (SMTP only, internal). Production: Azure Communication Services Email.
    mailpit = {
      image   = "docker.io/axllent/mailpit:v1.31.4", port = 1025, external = false, transport = "tcp", restricted = false
      cpu     = 0.25, memory = "0.5Gi", min = 1, max = 1, probe = null, registrations = false, openai = false
      command = null, env = {}, secrets = {}
    }
  }

  # (identity, secret) pairs: each identity may read only the secrets its app uses.
  secret_grants = merge(
    { for pair in flatten([
      for app, cfg in local.apps : [for s in distinct(values(cfg.secrets)) : { app = app, secret = s }]
    ]) : "${pair.app}/${pair.secret}" => pair },
    { "ingest/database-url" = { app = "ingest", secret = "database-url" } },
  )
}

resource "azurerm_user_assigned_identity" "app" {
  for_each            = toset(concat(keys(local.apps), ["ingest"]))
  name                = "id-${local.name}-${each.key}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_role_assignment" "app_secret" {
  for_each             = local.secret_grants
  scope                = azurerm_key_vault_secret.main[each.value.secret].resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app[each.value.app].principal_id
}

resource "azurerm_role_assignment" "acr_pull" {
  for_each             = azurerm_user_assigned_identity.app
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = each.value.principal_id
}

resource "azurerm_role_assignment" "openai_user" {
  for_each = toset(concat(
    [for app, cfg in local.apps : app if cfg.openai],
    var.embedding_provider == "azure-openai" ? ["ingest"] : [],
  ))
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app[each.key].principal_id
}

resource "azurerm_container_app" "app" {
  for_each                     = local.apps
  name                         = each.key # = the internal host name other apps use, as in docker-compose
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app[each.key].id]
  }

  dynamic "registry" {
    for_each = startswith(each.value.image, local.acr) ? [1] : []
    content {
      server   = local.acr
      identity = azurerm_user_assigned_identity.app[each.key].id
    }
  }

  dynamic "secret" {
    for_each = toset(distinct(values(each.value.secrets)))
    content {
      name                = secret.value
      key_vault_secret_id = azurerm_key_vault_secret.main[secret.value].versionless_id
      identity            = azurerm_user_assigned_identity.app[each.key].id
    }
  }

  ingress {
    external_enabled = each.value.external
    target_port      = each.value.port
    exposed_port     = each.value.transport == "tcp" ? each.value.port : null
    transport        = each.value.transport
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
    dynamic "ip_security_restriction" {
      for_each = each.value.restricted ? local.admin_ranges : []
      content {
        name             = "allow-${replace(replace(ip_security_restriction.value, ".", "-"), "/", "-")}"
        action           = "Allow"
        ip_address_range = ip_security_restriction.value
      }
    }
  }

  template {
    min_replicas = each.value.min
    max_replicas = each.value.max

    dynamic "init_container" {
      for_each = each.key == "n8n" ? [1] : []
      content {
        name    = "import-workflows"
        image   = each.value.image
        command = ["sh", "/import/import.sh"]
        cpu     = each.value.cpu
        memory  = each.value.memory
        dynamic "env" {
          for_each = each.value.env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = each.value.secrets
          content {
            name        = env.key
            secret_name = env.value
          }
        }
      }
    }

    container {
      name    = each.key
      image   = each.value.image
      command = each.value.command
      cpu     = each.value.cpu
      memory  = each.value.memory

      dynamic "env" {
        for_each = merge(
          each.value.env,
          { AZURE_CLIENT_ID = azurerm_user_assigned_identity.app[each.key].client_id }, # which identity to use
        )
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = each.value.secrets
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      dynamic "volume_mounts" {
        for_each = each.value.registrations ? [1] : []
        content {
          name = "registrations"
          path = "/data/registrations"
        }
      }
      dynamic "readiness_probe" {
        for_each = each.value.probe == null ? [] : [each.value.probe]
        content {
          transport = "HTTP"
          port      = each.value.port
          path      = readiness_probe.value
        }
      }
      dynamic "startup_probe" {
        for_each = each.key == "keycloak" ? [1] : []
        content {
          transport               = "HTTP"
          port                    = 9000 # Keycloak management port
          path                    = "/health/ready"
          interval_seconds        = 10
          failure_count_threshold = 30
        }
      }
    }

    dynamic "volume" {
      for_each = each.value.registrations ? [1] : []
      content {
        name          = "registrations"
        storage_type  = "AzureFile"
        storage_name  = azurerm_container_app_environment_storage.registrations.name
        mount_options = "uid=10001,gid=10001,dir_mode=0750,file_mode=0640"
      }
    }
  }

  depends_on = [
    time_sleep.rbac,
    azurerm_role_assignment.acr_pull,
    azurerm_postgresql_flexible_server_database.db,
    azurerm_postgresql_flexible_server_configuration.extensions,
  ]
}

# Migrations + catalog ingestion. Started by scripts/azure_up.sh after each deploy;
# idempotent, so re-running only processes changes.
resource "azurerm_container_app_job" "ingest" {
  name                         = "ingest"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 1800
  replica_retry_limit          = 1
  tags                         = local.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app["ingest"].id]
  }
  registry {
    server   = local.acr
    identity = azurerm_user_assigned_identity.app["ingest"].id
  }
  secret {
    name                = "database-url"
    key_vault_secret_id = azurerm_key_vault_secret.main["database-url"].versionless_id
    identity            = azurerm_user_assigned_identity.app["ingest"].id
  }
  template {
    container {
      name    = "ingest"
      image   = local.app_image
      command = ["sh", "-c", "alembic upgrade head && python -m nordlys_discovery.ingest"]
      cpu     = 1
      memory  = "2Gi"
      dynamic "env" {
        for_each = merge(local.app_env, {
          NORDLYS_REGISTRATIONS_DIR = "/data/registrations"
          AZURE_CLIENT_ID           = azurerm_user_assigned_identity.app["ingest"].client_id
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "NORDLYS_DATABASE_URL"
        secret_name = "database-url"
      }
      volume_mounts {
        name = "registrations"
        path = "/data/registrations"
      }
    }
    volume {
      name          = "registrations"
      storage_type  = "AzureFile"
      storage_name  = azurerm_container_app_environment_storage.registrations.name
      mount_options = "uid=10001,gid=10001,dir_mode=0750,file_mode=0640"
    }
  }
  depends_on = [time_sleep.rbac, azurerm_role_assignment.acr_pull, azurerm_postgresql_flexible_server_database.db]
}
