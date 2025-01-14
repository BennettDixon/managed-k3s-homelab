variable "role_name" {
  description = "Name of the IAM role"
  type        = string
}

variable "service_name" {
  description = "Service that assumes the IAM role"
  type        = string
}

variable "policy_name" {
  description = "Name of the IAM policy"
  type        = string
}

variable "policy_json" {
  description = "JSON policy document"
  type        = string
}
