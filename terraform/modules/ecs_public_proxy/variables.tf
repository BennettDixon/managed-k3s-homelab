variable "service_name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnets" {
  type = list(string)
}

variable "public_subnets" {
  type = list(string)
}

variable "container_image" {
  type = string
}

variable "container_port" {
  type = number
  default = 80
}

variable "desired_count" {
  type = number
  default = 1
}

variable "alb_arn" {
  type = string
}

variable "alb_security_group_id" {
  type = string
}

variable "listener_arn" {
  type = string
}

variable "tailscale_auth_key" {
  type        = string
  description = "The actual Tailscale auth key"
}

variable "tailscale_auth_secret_name" {
  type        = string
  description = "Name for the Tailscale auth secret in Secrets Manager"
  default     = "my/tailscale/auth"
}
