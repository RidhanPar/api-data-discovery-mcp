output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "acr_name" {
  value = azurerm_container_registry.main.name
}

output "key_vault" {
  value = azurerm_key_vault.main.name
}

output "mcp_url" {
  value = "https://${azurerm_container_app.app["mcp"].ingress[0].fqdn}/mcp"
}

output "n8n_url" {
  value = "https://${azurerm_container_app.app["n8n"].ingress[0].fqdn}/"
}

output "oidc_issuer" {
  value = local.issuer
}

output "azure_openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.main.workspace_id
}
