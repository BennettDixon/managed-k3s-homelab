output "secrets_arn" {
  value       = module.harbor_admin_password_secret.secret_arn
  description = "The ARN of the Secrets Manager secret for Harbor admin password"
}

output "harbor_admin_iam_role_arn" {
  value       = module.harbor_admin_iam.role_arn
  description = "The ARN of the IAM role for accessing Secrets Manager"
}

output "harbor_admin_iam_policy_arn" {
  value       = module.harbor_admin_iam.policy_arn
  description = "The ARN of the IAM policy attached to the IAM role"
}
