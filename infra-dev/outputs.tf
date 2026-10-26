output "gateway_url" {
  description = "Public URL of the Nginx gateway (HTTPS terminated by Container Apps)"
  value       = "https://${azurerm_container_app.gateway.ingress[0].fqdn}"
}

output "postgres_host" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}
