[
  {
    "name": "${container_name}",
    "image": "${container_image}",
    "essential": true,
    "portMappings": [
      {
        "containerPort": ${container_port},
        "hostPort": ${container_port},
        "protocol": "tcp"
      }
    ],
    "environment": [],
    "secrets": [
      {
        "name": "TAILSCALE_AUTH_KEY",
        "valueFrom": "${tailscale_secret_arn}"
      }
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/${container_name}",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "ecs"
      }
    }
  }
]
