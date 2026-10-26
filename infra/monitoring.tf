variable "monitoring_enabled" {
  description = "Opt in to probes and paid Azure Monitor log alerts after updated images are ready."
  type        = bool
  default     = false
}

variable "monitoring_action_group_ids" {
  description = "Existing Azure Monitor action groups; empty means portal-only alerts. Slack is not required."
  type        = list(string)
  default     = []
}

variable "monitoring_thresholds" {
  description = "Override alert thresholds by rule name (counts or seconds)."
  type        = map(number)
  default     = {}
  validation {
    condition     = alltrue([for value in values(var.monitoring_thresholds) : value >= 0])
    error_message = "Monitoring thresholds must be non-negative."
  }
}

locals {
  monitoring_source = <<-KQL
    ContainerAppConsoleLogs_CL
    | where ContainerAppName_s == "${azurerm_container_app.core.name}" or ContainerAppName_s == "${local.prefix}-scan-scheduler" or tostring(column_ifexists("ContainerJobName_s", "")) == "${local.prefix}-scan-scheduler"
    | extend m = parse_json(Log_s)
    | where tostring(m.schema) == "showdown.monitor.v1"
  KQL
  monitoring_rules = {
    api_errors        = { expression = "where m.event == 'api_request' and toint(m.status_code) >= 500 | summarize value=count()", threshold = 5, operator = "GreaterThanOrEqual" }
    api_unhealthy     = { expression = "where m.event == 'api_health' and toint(m.healthy) == 0 | summarize value=count()", threshold = 3, operator = "GreaterThanOrEqual" }
    worker_errors     = { expression = "where m.event in ('worker_failure', 'startup_failed') | summarize value=count()", threshold = 3, operator = "GreaterThanOrEqual" }
    worker_unhealthy  = { expression = "where m.event == 'worker_health' and toint(m.healthy) == 0 | summarize value=count()", threshold = 3, operator = "GreaterThanOrEqual" }
    scan_failures     = { expression = "where m.event == 'worker_health' | summarize value=max(toint(m.failed_count))", threshold = 3, operator = "GreaterThanOrEqual" }
    scan_duration     = { expression = "where m.event == 'worker_health' | summarize value=max(max_of(todouble(m.scan_duration_seconds), todouble(m.oldest_running_seconds)))", threshold = 1200, operator = "GreaterThanOrEqual" }
    queue_delayed     = { expression = "where m.event == 'worker_health' | summarize value=max(toint(m.old_queued_count))", threshold = 1, operator = "GreaterThanOrEqual" }
    missed_schedules  = { expression = "where m.event == 'worker_health' | summarize value=max(toint(m.overdue_schedules))", threshold = 1, operator = "GreaterThanOrEqual" }
    notifications     = { expression = "where m.event == 'worker_health' | summarize value=max(toint(m.notification_failures)+toint(m.notification_stuck)+toint(m.notification_overdue))", threshold = 1, operator = "GreaterThanOrEqual" }
    api_missing       = { expression = "where m.event == 'api_health' | summarize value=count()", threshold = 1, operator = "LessThan" }
    worker_missing    = { expression = "where m.event == 'worker_health' | summarize value=count()", threshold = 1, operator = "LessThan" }
    scheduler_missing = { expression = "where m.event == 'scheduler_completed' | summarize value=count()", threshold = 1, operator = "LessThan" }
    scheduler_errors  = { expression = "where m.event in ('scheduler_failed', 'scheduler_deferred') | summarize value=count()", threshold = 1, operator = "GreaterThanOrEqual" }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "showdown" {
  for_each = { for name, rule in local.monitoring_rules : name => rule
    if var.monitoring_enabled && (!startswith(name, "scheduler_") || var.scheduled_scanning_enabled)
  }
  name                    = "${local.prefix}-${replace(each.key, "_", "-")}"
  resource_group_name     = azurerm_resource_group.rg.name
  location                = azurerm_resource_group.rg.location
  scopes                  = [azurerm_log_analytics_workspace.logs.id]
  description             = "ShowDown operational telemetry: ${each.key}. No target or credential data."
  severity                = 2
  evaluation_frequency    = "PT5M"
  window_duration         = "PT30M"
  auto_mitigation_enabled = true
  enabled                 = true
  skip_query_validation   = false
  criteria {
    query                   = "${local.monitoring_source}\n| where TimeGenerated > ago(${each.key == "scheduler_missing" ? "30m" : "15m"})\n| ${each.value.expression}"
    time_aggregation_method = "Maximum"
    metric_measure_column   = "value"
    operator                = each.value.operator
    threshold               = lookup(var.monitoring_thresholds, each.key, each.value.threshold)
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }
  dynamic "action" {
    for_each = length(var.monitoring_action_group_ids) > 0 ? [1] : []
    content {
      action_groups = var.monitoring_action_group_ids
    }
  }
}
