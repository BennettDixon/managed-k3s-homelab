variable "ls_instance_name" {
  type    = string
  default = "tailscale-proxy"
}

variable "ls_availability_zone" {
  type    = string
  default = "us-east-1a"
}

variable "tailscale_auth_key" {
  type        = string
  description = "Tailscale auth key for tailscale up --authkey=..."
  sensitive   = true
  default     = ""  # Provide securely at runtime, e.g., via TF_VAR_tailscale_auth_key
}
