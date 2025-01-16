variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

variable "harbor_admin_password" {
  description = "Admin password for Harbor"
  type        = string
  sensitive   = true
}

variable "harbor_docker_pull_username" {
  description = "Username for Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "harbor_docker_pull_password" {
  description = "Password for Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "harbor_registry_domain" {
  description = "Domain for Harbor registry"
  type        = string
  default     = "harbor.internal"
}
