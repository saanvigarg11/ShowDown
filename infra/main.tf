locals {
  prefix = "asm"

  images = {
    nginx  = "ghcr.io/${var.ghcr_owner}/asm-nginx:${var.image_tag}"
    api    = "ghcr.io/${var.ghcr_owner}/asm-api:${var.image_tag}"
    worker = "ghcr.io/${var.ghcr_owner}/asm-worker:${var.image_tag}"
  }

  # asyncpg takes ssl=require in the query string; Azure Postgres enforces TLS.
  database_url = "postgresql+asyncpg://asmadmin:${random_password.pg.result}@${azurerm_postgresql_flexible_server.pg.fqdn}:5432/asm?ssl=require"
  redis_url    = "redis://localhost:6379/0" # Redis runs as a sidecar container in the core app
}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

# special=false keeps the password safe to embed in a URL without escaping
resource "random_password" "pg" {
  length  = 24
  special = false
}

resource "random_password" "api_secret" {
  length  = 32
  special = false
}

resource "azurerm_resource_group" "rg" {
  name     = "${local.prefix}-rg"
  location = var.location
}

# ---------- Logging (daily cap keeps ingestion costs bounded) ----------
resource "azurerm_log_analytics_workspace" "logs" {
  name                = "${local.prefix}-logs"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.5
}

resource "azurerm_container_app_environment" "env" {
  name                       = "${local.prefix}-env"
  location                   = azurerm_resource_group.rg.location
  resource_group_name        = azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id

  # A workload-profiles environment (the standard type) with only the
  # pay-per-use Consumption profile. Do NOT set min/max counts on it.
  # Our first apply (no workload_profile block) produced an "express"
  # environment, which is HTTP-only with no TCP and no service discovery
  # between apps. Declaring the profile gives a standard environment.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  # The provider shows a perpetual diff on this block (azurerm issue #31840)
  lifecycle {
    ignore_changes = [workload_profile]
  }
}

# ---------- PostgreSQL (the one non-trivial fixed cost) ----------
resource "azurerm_postgresql_flexible_server" "pg" {
  name                = "${local.prefix}-pg-${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location

  version                = "16"
  administrator_login    = "asmadmin"
  administrator_password = random_password.pg.result

  sku_name   = "B_Standard_B1ms"
  storage_mb = 32768

  backup_retention_days         = 7
  public_network_access_enabled = true

  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = "asm"
  server_id = azurerm_postgresql_flexible_server.pg.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# 0.0.0.0-0.0.0.0 is Azure's special "allow Azure-internal services" rule.
# Trade-off: any Azure tenant could *attempt* to connect; they still need the
# password and TLS. Tighten with VNet integration once the app works.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# ---------- Core app: API + worker + Redis as three containers in ONE app ----------
# Why one app: exposing Redis to other apps needs TCP ingress, which isn't
# available without a custom VNet. Containers in the same app share localhost,
# so the API and worker reach Redis at localhost:6379 with no ingress at all.
# Trade-off: they scale together, and the queue is in memory (a restart drops
# queued jobs; the scan rows in Postgres stay as "queued").
resource "azurerm_container_app" "core" {
  name                         = "${local.prefix}-core"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  secret {
    name  = "database-url"
    value = local.database_url
  }

  secret {
    name  = "api-secret"
    value = random_password.api_secret.result
  }

  dynamic "secret" {
    for_each = var.slack_webhook_url == "" ? [] : [1]
    content {
      name  = "slack-webhook"
      value = var.slack_webhook_url
    }
  }

  template {
    # Always on (the worker must keep polling) and exactly one replica, so the
    # in-memory Redis queue is never split across replicas.
    min_replicas = 1
    max_replicas = 1

    # total 1.0 vCPU / 2.0 Gi is a valid Container Apps size
    container {
      name   = "redis"
      image  = "redis:7-alpine"
      cpu    = 0.25
      memory = "0.5Gi"
    }

    container {
      name   = "api"
      image  = local.images.api
      cpu    = 0.25
      memory = "0.5Gi"

      dynamic "liveness_probe" {
        for_each = var.monitoring_enabled ? [1] : []
        content {
          transport = "HTTP"
          port = 8000
          path = "/health/live"
          initial_delay = 30
          interval_seconds = 30
          timeout = 5
          failure_count_threshold = 3
        }
      }
      dynamic "readiness_probe" {
        for_each = var.monitoring_enabled ? [1] : []
        content {
          transport = "HTTP"
          port = 8000
          path = "/health/ready"
          interval_seconds = 30
          timeout = 5
          failure_count_threshold = 3
        }
      }

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "API_SECRET_KEY"
        secret_name = "api-secret"
      }
      env {
        name  = "REDIS_URL"
        value = local.redis_url
      }
      env {
        name  = "SCAN_QUEUE_NAME"
        value = "scan_jobs"
      }
    }

    container {
      name   = "worker"
      image  = local.images.worker
      cpu    = 0.5
      memory = "1Gi"
      # 8001 is only for internal health probes; no ingress route is added.
      dynamic "liveness_probe" {
        for_each = var.monitoring_enabled ? [1] : []
        content {
          transport = "HTTP"
          port = 8001
          path = "/health/live"
          initial_delay = 30
          interval_seconds = 30
          timeout = 5
          failure_count_threshold = 3
        }
      }
      dynamic "readiness_probe" {
        for_each = var.monitoring_enabled ? [1] : []
        content {
          transport = "HTTP"
          port = 8001
          path = "/health/ready"
          interval_seconds = 30
          timeout = 5
          failure_count_threshold = 3
        }
      }

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name  = "REDIS_URL"
        value = local.redis_url
      }
      env {
        name  = "SCAN_QUEUE_NAME"
        value = "scan_jobs"
      }

      dynamic "env" {
        for_each = var.slack_webhook_url == "" ? [] : [1]
        content {
          name        = "SLACK_WEBHOOK_URL"
          secret_name = "slack-webhook"
        }
      }
    }
  }

  # Ingress targets the API container (the only one that listens)
  ingress {
    external_enabled = false
    target_port      = 8000
    # Nginx talks to the API over plain HTTP inside the environment; without
    # this the ingress answers http:// with a redirect to https://.
    allow_insecure_connections = true

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  depends_on = [
    azurerm_postgresql_flexible_server_database.db,
    azurerm_postgresql_flexible_server_firewall_rule.azure_services,
  ]
}

# ---------- Nginx gateway (the only public entry point) ----------
resource "azurerm_container_app" "gateway" {
  name                         = "${local.prefix}-gateway"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name   = "nginx"
      image  = local.images.nginx
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "API_HOST"
        value = azurerm_container_app.core.ingress[0].fqdn
      }
      env {
        name  = "API_PORT"
        value = "80"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}
