variable "secret_name" {
  description = "Name of the secret"
  type        = string
}

variable "description" {
  description = "Description of the secret"
  type        = string
  default     = ""
}

variable "secret_value" {
  description = "The actual secret value"
  type        = string
  sensitive   = true
}
