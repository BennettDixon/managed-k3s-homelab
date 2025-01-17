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

variable "harbor_registry_domain" {
  description = "Root domain for harbor registries"
  type        = string
  default     = "harbor.internal"
}

variable "default_harbor_docker_pull_username" {
  description = "Username for default registry - Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "default_harbor_docker_pull_password" {
  description = "Password for default registry - Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "default_harbor_docker_pull_email" {
  description = "Email for default registry -  Harbor Docker pull"
  type        = string
  default     = "default@example.com"
}

variable "personal_site_harbor_docker_pull_username" {
  description = "Username for personal site registry - Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "personal_site_harbor_docker_pull_password" {
  description = "Password for personal site registry -  Harbor Docker pull"
  type        = string
  sensitive   = true
}

variable "personal_site_harbor_docker_pull_email" {
  description = "Email for personal site registry -  Harbor Docker pull account"
  type        = string
  default     = "default@example.com"
}
