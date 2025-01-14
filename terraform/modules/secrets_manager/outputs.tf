output "secret_arn" {
  value       = aws_secretsmanager_secret.secret.arn
  description = "ARN of the Secrets Manager secret"
}
