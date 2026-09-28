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

# knowledge-mcp secrets (spec: docs/specs/knowledge-mcp.md §2/§8; consumed by
# the ExternalSecrets in apps/base/knowledge-mcp/)
module "knowledge_mcp_caller_tokens_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_knowledge_mcp_caller_tokens"
  description = "knowledge-mcp caller-token JSON map (caller_id -> token); policy lives in the repo registry. The n8n-reingest value must also live in the n8n LXC env as KNOWLEDGE_REINGEST_TOKEN"
  # The WHOLE secret value is the map: the ExternalSecret reads it with no
  # `property`, so the pod receives {"operator": ..., "n8n-reingest": ...}
  # verbatim as KNOWLEDGE_CALLER_TOKENS (house target auth shape, SIGN-OFF 2).
  # Adding a caller = one more entry here + a `callers:` line in corpora.yaml.
  secret_value = jsonencode({
    operator       = var.knowledge_mcp_operator_token
    "n8n-reingest" = var.knowledge_mcp_n8n_reingest_token
  })
}

module "knowledge_harbor_docker_pull_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_knowledge"
  description = "Pull-only Harbor robot for the knowledge project (knowledge-mcp images)"
  secret_value = jsonencode({
    username = var.knowledge_harbor_docker_pull_username
    password = var.knowledge_harbor_docker_pull_password
    registry = var.harbor_registry_domain
  })
}

# gateway secrets (spec: docs/specs/gateway.md §7; consumed by the
# ExternalSecrets in apps/base/gateway/). Onboarding, rotation and every
# out-of-band holder: docs/runbooks/gateway.md. All three ARNs are in the ESO
# reader policy (iam-external-secrets.tf) — the 2026-09-09 rule: same PR, or
# ESO gets AccessDenied for the new entry.
module "gateway_caller_tokens_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_gateway_caller_tokens"
  description = "gateway caller-token JSON map (caller_id -> token); policy lives in the repo registry apps/base/gateway/registry.yaml. Out-of-band holders: the operator value in the operator workbench account env as GATEWAY_TOKEN (handed to an OpenAI-speaking tool only per tool, together with OPENAI_BASE_URL, never as a global OPENAI_API_KEY); the n8n-executor value in the n8n LXC env as GATEWAY_EXECUTOR_TOKEN (slice 3). The lane-agent token is minted with the deferred subscription lane, not here"
  # The WHOLE secret value is the map: the ExternalSecret reads it with no
  # `property`, so the pod receives {"operator": ..., "n8n-executor": ...}
  # verbatim as GATEWAY_CALLER_TOKENS (house auth shape, knowledge-mcp
  # precedent). Adding a caller = one more entry here + a `callers:` line in
  # registry.yaml — the PR is where a human reads what they grant.
  secret_value = jsonencode({
    operator       = var.gateway_operator_token
    "n8n-executor" = var.gateway_n8n_executor_token
  })
}

module "gateway_anthropic_api_key_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_gateway_anthropic_api_key"
  description = "Anthropic API key for the gateway's metered lane, minted in the dedicated Console workspace homelab-gateway whose monthly spend limit is the outer bound (spec §7.3, set BEFORE the key is minted). Holders, exhaustively: this entry, the gateway pod (env ANTHROPIC_API_KEY via ESO in namespace gateway), nobody else — never a worker, never n8n, never CI"
  secret_value = jsonencode({
    api_key = var.gateway_anthropic_api_key
  })
}

module "gateway_harbor_docker_pull_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_gateway"
  description = "Pull-only Harbor robot for the gateway project (gateway images); holders: this entry and the imagePullSecret in namespace gateway"
  secret_value = jsonencode({
    username = var.gateway_harbor_docker_pull_username
    password = var.gateway_harbor_docker_pull_password
    registry = var.harbor_registry_domain
  })
}

# ---------------------------------------------------------------------------
# card-sorter: inventory + identify on k3s (private repo card-sorter,
# docs/specs/inventory.md §2, §1b, §6). Three entries, all read by ESO in
# namespace card-sorter; the ARNs are in iam-external-secrets.tf.
# ---------------------------------------------------------------------------
module "card_sorter_caller_tokens_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_card_sorter_caller_tokens"
  description = "card-sorter caller-token JSON map (caller_id -> token) for inventory and identify; class per caller lives in apps/base/card-sorter/callers.yaml. Out-of-band holders: the sorter-01 value in /etc/sorter-agent/token on the machine's Pi; the bennett value in the operator workbench env as INVENTORY_TOKEN; the identify value only in the identify pod, the inventory value only in the inventory pod (each calls the other, PR 2)"
  # The WHOLE value is the map: inventory and identify read it with no
  # `property` (house auth shape); identify's own entry is also read by
  # property for its calls back to inventory. Adding a caller = one more
  # entry here + a line in callers.yaml.
  secret_value = jsonencode({
    "sorter-01" = var.card_sorter_sorter01_token
    bennett     = var.card_sorter_operator_token
    identify    = var.card_sorter_identify_token
    inventory   = var.card_sorter_inventory_token
  })
}

module "card_sorter_litestream_sftp_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_card_sorter_litestream_sftp"
  description = "Private key of the NAS user litestrm (TrueNAS caps the name), whose home is BulkPoolZ2/artifacts/card-sorter/litestream/litestrm and nothing else; holders: this entry and the inventory pod's /ssh mount (Litestream sidecar, inventory spec §1b)"
  secret_value = jsonencode({
    user        = "litestrm"
    private_key = var.card_sorter_litestream_private_key
  })
}

module "card_sorter_harbor_docker_pull_secret" {
  source      = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull_card_sorter"
  description = "Pull-only Harbor robot for the card-sorter project (inventory and identify images); holders: this entry and the imagePullSecret in namespace card-sorter"
  secret_value = jsonencode({
    username = var.card_sorter_harbor_docker_pull_username
    password = var.card_sorter_harbor_docker_pull_password
    registry = var.harbor_registry_domain
  })
}
