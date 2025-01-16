output "role_arn" {
  value       = aws_iam_role.secrets_role.arn
  description = "ARN of the IAM role"
}

output "policy_arn" {
  value       = aws_iam_policy.secrets_access_policy.arn
  description = "ARN of the attached policy"
}
