variable "vpc_id" {
  type = string
}

variable "public_subnets" {
  type = list(string)
}

variable "environment_name" {
  type = string
}

variable "domain_name" {
  type = string
}

variable "certificate_arn" {
  type = string
}
