# -----------------------------------------------------------------------------
# ECS Cluster
# -----------------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = "${var.service_name}-cluster"
}

# -----------------------------------------------------------------------------
# IAM Role for ECS Task Execution
# -----------------------------------------------------------------------------
resource "aws_iam_role" "task_execution_role" {
  name               = "${var.service_name}-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_role.json
}

data "aws_iam_policy_document" "ecs_task_execution_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "task_exec_attach" {
  role       = aws_iam_role.task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# -----------------------------------------------------------------------------
# Allow ECS Task to read from Secrets Manager
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "secretsmanager_read" {
  statement {
    actions = [
      "secretsmanager:GetSecretValue"
    ]
    # If you want to restrict to just this secret, reference the secret's ARN (see below)
    # This is an example; best practice is to reference the actual ARN once you create it.
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_secretsmanager_policy" {
  name = "${var.service_name}-secretsmanager-policy"
  role = aws_iam_role.task_execution_role.id
  policy = data.aws_iam_policy_document.secretsmanager_read.json
}

# -----------------------------------------------------------------------------
# Store Tailscale Auth Key in AWS Secrets Manager
# -----------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "tailscale_auth_secret" {
  name        = var.tailscale_auth_secret_name  # e.g. "my/tailscale/auth"
  description = "Tailscale auth key"
}

resource "aws_secretsmanager_secret_version" "tailscale_auth_secret_version" {
  secret_id     = aws_secretsmanager_secret.tailscale_auth_secret.id
  secret_string = var.tailscale_auth_key
}

# -----------------------------------------------------------------------------
# ECS Task Definition
# -----------------------------------------------------------------------------
resource "aws_ecs_task_definition" "this" {
  family                   = var.service_name
  cpu                      = 256
  memory                   = 512
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_execution_role.arn

  container_definitions = templatefile("${path.module}/container_definitions.json.tpl", {
    container_name      = var.service_name
    container_image     = var.container_image
    container_port      = var.container_port

    # We'll pass in the Secrets Manager secret ARN so the container can retrieve TAILSCALE_AUTH_KEY
    tailscale_secret_arn = aws_secretsmanager_secret_version.tailscale_auth_secret_version.arn
  })
}

# -----------------------------------------------------------------------------
# ECS Service
# -----------------------------------------------------------------------------
resource "aws_ecs_service" "this" {
  name            = var.service_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnets
    assign_public_ip = false
    security_groups  = [aws_security_group.service_sg.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = var.service_name
    container_port   = var.container_port
  }

  depends_on = [
    aws_lb_listener_rule.this
  ]
}

# -----------------------------------------------------------------------------
# Security Group for ECS Tasks (Allow inbound from ALB on container port)
# -----------------------------------------------------------------------------
resource "aws_security_group" "service_sg" {
  name        = "${var.service_name}-sg"
  vpc_id      = var.vpc_id
  description = "Allow inbound from ALB on container port"

  ingress {
    from_port        = var.container_port
    to_port          = var.container_port
    protocol         = "tcp"
    security_groups  = [var.alb_security_group_id]
    description      = "Allow inbound from ALB"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# -----------------------------------------------------------------------------
# Target Group
# -----------------------------------------------------------------------------
resource "aws_lb_target_group" "this" {
  name        = "${var.service_name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path = "/"
    port = "traffic-port"
  }
}

# -----------------------------------------------------------------------------
# Listener Rule to forward traffic to the service’s Target Group
# -----------------------------------------------------------------------------
resource "aws_lb_listener_rule" "this" {
  listener_arn = var.listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }

  condition {
    host_header {
      values = ["${var.service_name}.${var.domain_name}"]
    }
  }
}
