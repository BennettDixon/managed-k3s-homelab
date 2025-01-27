# I initially wanted to create a HA (high availability) public proxy
# with an ECS Fargate service, but it was going to be costly for just
# my homelab. I chose to instead use lightsail, but left this code
# in place in-case I want to scale up in the future.
# This HA proxy is untested and may not work as-is.


# 1) Create or import a VPC (using our custom module or a known one)
# module "vpc" {
#   source = "./modules/vpc"

#   vpc_cidr         = var.vpc_cidr
#   public_subnets   = var.public_subnets
#   private_subnets  = var.private_subnets
# }

# # 2) Create an ALB (application load balancer)
# module "alb" {
#   source = "./modules/alb"

#   vpc_id               = module.vpc.vpc_id
#   public_subnets       = module.vpc.public_subnets
#   environment_name     = "public-proxy"
#   domain_name          = var.public_proxy_domain_name      # e.g. "mydomain.com"
#   certificate_arn      = var.public_proxy_certificate_arn  # from ACM
# }

# # 3) Create ECS Fargate cluster & service
# module "ecs_service" {
#   source = "./modules/ecs_public_proxy"

#   service_name         = "tailscale-nginx-public-proxy"
#   vpc_id               = module.vpc.vpc_id
#   private_subnets      = module.vpc.private_subnets
#   public_subnets       = module.vpc.public_subnets
#   container_image      = var.public_proxy_container_image  # ECR image URI
#   container_port       = var.public_proxy_container_port   # Typically 80
#   tailscale_auth_key = var.tailscale_auth_key
#   tailscale_auth_secret_name = "tailscale_nginx_public_proxy_ts_auth_key"
#   desired_count        = var.desired_public_proxy_count

#   # ALB integration
#   alb_arn              = module.alb.alb_arn
#   alb_security_group_id= module.alb.alb_sg_id
#   listener_arn         = module.alb.listener_arn
# }
