variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into."
}

variable "location" {
  type        = string
  default     = "swedencentral"
  description = "Region. Must offer Container Apps, PostgreSQL Flexible Server and the Azure OpenAI models below."
}

variable "prefix" {
  type        = string
  default     = "nordlys"
  description = "Short lowercase name used in resource names."
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,10}$", var.prefix))
    error_message = "3-11 lowercase letters/digits, starting with a letter."
  }
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the images built into ACR by scripts/azure_up.sh (the git commit)."
}

variable "budget_amount" {
  type        = number
  default     = 30
  description = "Monthly budget for the resource group in the billing currency. Alerts at 50/80/100% actual and 100% forecast."
}

variable "budget_emails" {
  type        = list(string)
  description = "Who receives budget alerts."
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Public CIDRs allowed to reach n8n (editor and approval links) and the Keycloak admin console, e.g. your office or VPN egress."
}

variable "chat_model" {
  type = object({ name = string, version = string, sku = string, capacity = number })
  default = {
    name     = "gpt-4.1-mini"
    version  = "2025-04-14"
    sku      = "GlobalStandard"
    capacity = 20 # thousands of tokens per minute
  }
  description = "Azure OpenAI chat deployment used by the agent and the registration classifier."
}

variable "embedding_model" {
  type = object({ name = string, version = string, sku = string, capacity = number })
  default = {
    name     = "text-embedding-3-small"
    version  = "1"
    sku      = "GlobalStandard"
    capacity = 20
  }
}

variable "embedding_provider" {
  type        = string
  default     = "azure-openai"
  description = "azure-openai (via managed identity) or onnx-local (the model baked into the image; no per-call cost, reproduces the local eval numbers)."
  validation {
    condition     = contains(["azure-openai", "onnx-local"], var.embedding_provider)
    error_message = "azure-openai or onnx-local."
  }
}

variable "approval_timeout_minutes" {
  type    = number
  default = 1440
}
