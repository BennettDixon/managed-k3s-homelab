resource "aws_iam_role" "secrets_role" {
  name = var.role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect    = "Allow",
        Principal = {
          Service = var.service_name
        },
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "secrets_access_policy" {
  name   = var.policy_name
  policy = var.policy_json
}

resource "aws_iam_role_policy_attachment" "attach_policy" {
  role       = aws_iam_role.secrets_role.name
  policy_arn = aws_iam_policy.secrets_access_policy.arn
}
