variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

variable "tailscale_oauth_client_id" {
  description = "Tailscale OAuth client ID"
  type        = string
  sensitive   = true
}

variable "tailscale_oauth_client_secret" {
  description = "Tailscale OAuth client secret"
  type        = string
  sensitive   = true
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

variable "contact_me_gmail_username" {
  description = "Gmail username for nodemailer on personal site contact form"
  type        = string
  sensitive   = true
}

variable "contact_me_gmail_password" {
  description = "Gmail password for nodemailer on personal site contact form"
  type        = string
  sensitive   = true
}

# Lightsail variables (low availability cheap proxy)
variable "ls_instance_name" {
  type    = string
  default = "tailscale-proxy"
}

variable "ls_availability_zone" {
  type    = string
  default = "us-east-1a"
}

# TS Auth key (used for both Lightsail and HA Public Proxy)
variable "tailscale_auth_key" {
  type        = string
  description = "Tailscale auth key for tailscale up --authkey=..."
  sensitive   = true
  default     = ""  # Provide securely at runtime, e.g., via TF_VAR_tailscale_auth_key
}

# Variables for example ha-public-proxy

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type = string
  default = "10.0.0.0/16"
}

variable "public_subnets" {
  description = "List of public subnet CIDR blocks"
  type = list(string)
  default = ["10.0.0.0/24", "10.0.1.0/24"]
}

variable "private_subnets" {
  description = "List of private subnet CIDR blocks"
  type = list(string)
  default = ["10.0.2.0/24", "10.0.3.0/24"]
}

variable "desired_public_proxy_count" {
  description = "Desired number of public proxy instances for ha-public-proxy fargate deployment"
  type = number
  default = 1
}

# In practice we wouldn't set a default for some of these
# variables, but to not bug the main code since this is an example
variable "public_proxy_container_image" {
  type = string
  default = "not-set"
}

variable "public_proxy_container_port" {
  type = number
  default = 80
}

variable "public_proxy_domain_name" {
  type = string
  default = "example.com"
}

variable "public_proxy_certificate_arn" {
  type = string
  default = "not-set"
}

