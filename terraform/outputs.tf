output "harbor_admin_secret_arn" {
  value       = module.harbor_admin_password_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Harbor admin password"
}

output "default_harbor_docker_pull_secret_arn" {
  value       = module.default_harbor_docker_pull_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Harbor Docker pull"
}

output "personal_site_harbor_docker_pull_secret_arn" {
  value       = module.personal_site_harbor_docker_pull_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Harbor Docker pull"
}

output "cluster_secret_reader_iam_role_arn" {
  value       = module.cluster_secret_reader.role_arn
  description = "The ARN of the IAM role for accessing Secrets Manager"
}

output "cluster_secret_reader_iam_policy_arn" {
  value       = module.cluster_secret_reader.policy_arn
  description = "The ARN of the IAM policy attached to the IAM role"
}

output "tailscale_oauth_secret_arn" {
  value       = module.tailscale_oauth_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Tailscale OAuth"
}

output "contact_me_gmail_account_details_secret_arn" {
  value       = module.contact_me_gmail_account_details_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Gmail account details"
}

# external-secrets (ESO) reader identity — see terraform/iam-external-secrets.tf
# and docs/runbooks/external-secrets-iam.md. The two access-key outputs are the
# values the operator writes into the cluster `aws-creds` Secret at cutover;
# read them with `terraform output -raw <name>` (they are sensitive, so bare
# `terraform output` prints "<sensitive>").
output "external_secrets_reader_user_name" {
  value       = aws_iam_user.external_secrets_reader.name
  description = "Name of the terraform-managed IAM user ESO authenticates as"
}

output "external_secrets_reader_iam_policy_arn" {
  value       = aws_iam_policy.external_secrets_reader.arn
  description = "ARN of the least-privilege read-only Secrets Manager policy attached to the ESO reader user"
}

output "external_secrets_reader_access_key_id" {
  value       = aws_iam_access_key.external_secrets_reader.id
  description = "Access key ID for the ESO reader user; goes into the cluster aws-creds Secret key aws_access_key_id at cutover"
  sensitive   = true
}

output "external_secrets_reader_secret_access_key" {
  value       = aws_iam_access_key.external_secrets_reader.secret
  description = "Secret access key for the ESO reader user; goes into the cluster aws-creds Secret key aws_secret_access_key at cutover"
  sensitive   = true
}
