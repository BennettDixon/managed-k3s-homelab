# external-secrets (ESO) IAM identity — brings the previously console-created,
# unmanaged IAM user behind the cluster's `aws-creds` Secret under terraform.
#
# WHY: ESO (ClusterSecretStore `aws-secrets-manager`, wired in
# infrastructure/configs/secret-stores.yaml) authenticates to Secrets Manager
# with a STATIC IAM user access key held in the k8s Secret `aws-creds`
# (namespace external-secrets, keys aws_access_key_id / aws_secret_access_key).
# The pre-existing identity was created in the console and carried the AWS
# managed policy `SecretsManagerReadWrite` — account-wide read AND write to
# every secret in the account. Far broader than ESO needs, and entirely
# unmanaged (a single ~600-day-old access key). ESO in this repo only ever
# performs GetSecretValue (no PushSecret resources exist anywhere under apps/
# or infrastructure/), so this replacement identity is scoped read-only to
# exactly the 13 secrets ESO consumes.
#
# CUTOVER IS A GATED OPERATOR STEP — see docs/runbooks/external-secrets-iam.md.
# Merging this changes NOTHING live: nothing reconciles from terraform, and the
# cluster keeps using the old key until the operator runs a TARGETED apply and
# swaps the two values in the `aws-creds` Secret out of band. The `aws-creds`
# Secret is applied out of band on purpose (chicken/egg: ESO cannot source its
# own bootstrap credential), which is why this PR does not touch it.

# Dedicated least-privilege policy for the ESO reader. Actions are read-only:
#   - GetSecretValue  : the call ESO makes on every ExternalSecret sync
#   - DescribeSecret  : version/metadata resolution
# No write actions (PutSecretValue/CreateSecret/etc.) — ESO issues no
# PushSecret in this repo. Resource ARNs come from each secret module's
# `.secret_arn` output (same style as module.cluster_secret_reader) so the AWS
# account id never appears as a literal in this PUBLIC repo, and the allowed
# set stays in lockstep with the modules that actually define the secrets.
resource "aws_iam_policy" "external_secrets_reader" {
  name        = "k3s-external-secrets-reader-policy"
  description = "Read-only Secrets Manager access for the k3s external-secrets (ESO) ClusterSecretStore; scoped to exactly the secrets ESO consumes"
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ],
        Resource = [
          module.harbor_admin_password_secret.secret_arn,
          module.default_harbor_docker_pull_secret.secret_arn,
          module.personal_site_harbor_docker_pull_secret.secret_arn,
          module.jobs_harbor_docker_pull_secret.secret_arn,
          module.knowledge_harbor_docker_pull_secret.secret_arn,
          module.jobs_mcp_bearer_token_secret.secret_arn,
          module.jobs_mcp_webhook_secret.secret_arn,
          module.jobs_mcp_n8n_api_key_secret.secret_arn,
          module.knowledge_mcp_caller_tokens_secret.secret_arn,
          module.alerts_webhook_secret.secret_arn,
          module.tailscale_oauth_secret.secret_arn,
          module.personal_site_recaptcha_secret.secret_arn,
          module.contact_me_gmail_account_details_secret.secret_arn
        ]
      }
    ]
  })
}

# ESO authenticates as an IAM USER with a static access key (not an assumable
# role), so this is a plain user — NOT the ./modules/iam module, which only
# builds a role with an sts:AssumeRole trust policy for a service principal.
# Programmatic access only; no console login profile is created.
resource "aws_iam_user" "external_secrets_reader" {
  name = "k3s-external-secrets-reader"
}

resource "aws_iam_user_policy_attachment" "external_secrets_reader" {
  user       = aws_iam_user.external_secrets_reader.name
  policy_arn = aws_iam_policy.external_secrets_reader.arn
}

# The static access key ESO presents. Both halves land in terraform state (the
# repo's existing model for generated secret material — state lives in the S3
# backend, never in git); the operator reads them at cutover via
# `terraform output -raw ...` and writes them into the cluster `aws-creds`
# Secret. See the outputs in outputs.tf and the runbook cutover procedure.
resource "aws_iam_access_key" "external_secrets_reader" {
  user = aws_iam_user.external_secrets_reader.name
}
