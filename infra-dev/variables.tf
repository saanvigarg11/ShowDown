variable "subscription_id" {
  description = "Azure subscription ID (az account show --query id -o tsv)"
  type        = string
}

variable "location" {
  description = "Azure region. MUST be one of the regions your student subscription's 'Allowed resource deployment regions' policy permits."
  type        = string
}

variable "ghcr_owner" {
  description = "Lowercase GitHub username/org that owns the ghcr.io images"
  type        = string
}

variable "image_tag" {
  description = "Image tag to deploy. Use a commit SHA, not 'latest': Container Apps will not roll a new revision when 'latest' changes underneath it."
  type        = string
  default     = "latest"
}

variable "slack_webhook_url" {
  description = "Optional Slack incoming-webhook URL for change alerts"
  type        = string
  default     = ""
  sensitive   = true
}
