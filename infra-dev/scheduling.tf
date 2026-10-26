variable "scheduled_scanning_enabled" {
  description = "Opt in only after deploying current API/worker and migrating scan_schedules."
  type        = bool
  default     = false
}

resource "azurerm_container_app_job" "scan_scheduler" {
  count                        = var.scheduled_scanning_enabled ? 1 : 0
  name                         = "${local.prefix}-scan-scheduler"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 1200
  replica_retry_limit          = 2

  schedule_trigger_config {
    cron_expression          = "*/5 * * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  secret {
    name  = "database-url"
    value = local.database_url
  }

  template {
    container {
      name    = "scheduler"
      image   = local.images.api
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "app.scheduler"]
      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      # Required by shared API settings; dispatcher never connects to Redis.
      env {
        name  = "REDIS_URL"
        value = local.redis_url
      }
    }
  }
  depends_on = [azurerm_container_app.core]
}
