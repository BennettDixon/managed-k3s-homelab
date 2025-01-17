module "harbor_admin_password_secret" {
  source       = "./modules/secrets_manager"
  secret_name  = "k3s_harbor_admin_password"
  description  = "Admin password for Harbor"
  secret_value = var.harbor_admin_password
}

module "harbor_docker_pull_secret" {
  source = "./modules/secrets_manager"
  secret_name = "k3s_harbor_docker_pull"
  description = "Docker pull secret for Harbor"
  secret_value = jsonencode({
    username = var.harbor_docker_pull_username
    password = var.harbor_docker_pull_password
    registry = var.harbor_registry_domain
    email = var.harbor_docker_pull_email
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
          module.harbor_docker_pull_secret.secret_arn,
        ]
      }
    ]
  })
}
