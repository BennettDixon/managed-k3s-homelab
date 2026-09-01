# Create a lightsail instance for our public proxy
module "public_proxy_tailscale_lightsail" {
  source = "./modules/public_proxy_lightsail"

  ls_instance_name = var.ls_instance_name
  ls_availability_zone = var.ls_availability_zone
  tailscale_auth_key = var.tailscale_auth_key
}

# Create secrets
module "harbor_admin_password_secret" {
  source       = "./modules/secrets_manager"
  secret_name  = "k3s_harbor_admin_password"
  description  = "Admin password for Harbor dashboard"
  secret_value = var.harbor_admin_password
}

module "default_harbor_docker_pull_secret" {
  source = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_default"
  description = "Default registry login info for Harbor"
  secret_value = jsonencode({
    username = var.default_harbor_docker_pull_username
    password = var.default_harbor_docker_pull_password
    email = var.default_harbor_docker_pull_email
    registry = var.harbor_registry_domain
  })
}

module "personal_site_harbor_docker_pull_secret" {
  source = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_personal_site"
  description = "Personal site registry login info for Harbor"
  secret_value = jsonencode({
    username = var.personal_site_harbor_docker_pull_username
    password = var.personal_site_harbor_docker_pull_password
    email = var.personal_site_harbor_docker_pull_email
    registry = var.harbor_registry_domain
  })
}

module "tailscale_oauth_secret" {
  source = "./modules/secrets_manager"
  secret_name = "k3s_tailscale_oauth"
  description = "Tailscale OAuth secret"
  secret_value = jsonencode({
    client_id = var.tailscale_oauth_client_id
    client_secret = var.tailscale_oauth_client_secret
  })
}

# Secret for recaptcha verification on personal site
module "personal_site_recaptcha_secret" {
  source = "./modules/secrets_manager"
  secret_name = "recaptcha_personal_site_keys"
  description = "Recaptcha secret key for personal site"
  secret_value = jsonencode({
    secret_server_key = var.personal_site_recaptcha_secret_key
    public_client_key = var.personal_site_recaptcha_public_key
  })
}

module "contact_me_gmail_account_details_secret" {
  source = "./modules/secrets_manager"
  secret_name = "contact_me_gmail_account_details"
  description = "Contact me Gmail account details for personal site nodemailer"
  secret_value = jsonencode({
    username = var.contact_me_gmail_username
    password = var.contact_me_gmail_password
  })
}

module "cluster_secret_reader" {
  source       = "./modules/iam"
  role_name    = "k3s-cluster-secrets-access-role"
  service_name = "ecs-tasks.amazonaws.com"
  policy_name  = "k3s-cluster-secrets-access-policy"
  policy_json  = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["secretsmanager:GetSecretValue"],
        Resource = [
          module.harbor_admin_password_secret.secret_arn,
          module.default_harbor_docker_pull_secret.secret_arn,
          module.personal_site_harbor_docker_pull_secret.secret_arn
        ]
      }
    ]
  })
}

# INTERNAL RESOURCES
# These resources are defined for my personal projects, but the terraform
# Code lives here as it is my main homelab terraform repo.


# AGENT CRAWLER
module "agent_crawler_s3_dev_bucket" {
  source            = "./modules/s3"
  bucket_name       = "agent-crawler-dixon-devs"
  acl               = "private"
  versioning_enabled = true
  tags              = {
    Environment = "dev"
    Project     = "example"
  }
  enable_encryption = true
  sse_algorithm     = "AES256"
}

module "agent_crawler_s3_dev_access" {
  source       = "./modules/iam"
  role_name    = "agent-crawler-s3-dev-access-role"
  service_name = "ecs-tasks.amazonaws.com"
  policy_name  = "agent-crawler-s3-dev-access-policy"
  policy_json  = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["s3:ListBucket"],
        Resource = [module.agent_crawler_s3_dev_bucket.bucket_arn]
      },
      {
        Effect   = "Allow",
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        Resource = ["${module.agent_crawler_s3_dev_bucket.bucket_arn}/*"]
      }
    ]
  })
}

resource "aws_iam_user" "agent_crawler_s3_dev_user" {
  name = "agent-crawler-s3-dev-user"
}

resource "aws_iam_user_policy_attachment" "agent_crawler_s3_dev_user_access" {
  user       = aws_iam_user.agent_crawler_s3_dev_user.name
  policy_arn = module.agent_crawler_s3_dev_access.policy_arn
}


# jobs-mcp secrets (spec: docs/specs/jobs-mcp.md §12; consumed by the
# ExternalSecrets in apps/base/jobs-mcp/)
module "jobs_mcp_bearer_token_secret" {
  source       = "./modules/secrets_manager"
  secret_name  = "k3s_jobs_mcp_bearer_token"
  description  = "jobs-mcp MCP bearer token (single v1 caller credential)"
  secret_value = jsonencode({
    token = var.jobs_mcp_bearer_token
  })
}

module "jobs_mcp_webhook_secret" {
  source       = "./modules/secrets_manager"
  secret_name  = "k3s_jobs_mcp_webhook_secret"
  description  = "Shared secret on jobs-mcp -> n8n webhook calls; same value must live in the n8n LXC env as JOBS_WEBHOOK_SECRET"
  secret_value = jsonencode({
    secret = var.jobs_mcp_webhook_secret
  })
}

module "jobs_mcp_n8n_api_key_secret" {
  source       = "./modules/secrets_manager"
  secret_name  = "k3s_jobs_mcp_n8n_api_key"
  description  = "Dedicated n8n API key for jobs-mcp startup checks (n8n UI key labeled jobs-mcp)"
  secret_value = jsonencode({
    api_key = var.jobs_mcp_n8n_api_key
  })
}

# Alert delivery (docs/runbooks/alerts.md): Alertmanager -> n8n webhook.
module "alerts_webhook_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_alerts_webhook_secret"
  description = "Bearer credential Alertmanager presents to the n8n alerts webhook; same value must live in the n8n LXC env as ALERTS_WEBHOOK_SECRET"
  secret_value = jsonencode({
    secret = var.alerts_webhook_secret
  })
}

module "jobs_harbor_docker_pull_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_jobs"
  description = "Pull-only Harbor robot for the jobs project (jobs-mcp images)"
  secret_value = jsonencode({
    username = var.jobs_harbor_docker_pull_username
    password = var.jobs_harbor_docker_pull_password
    registry = var.harbor_registry_domain
  })
}
