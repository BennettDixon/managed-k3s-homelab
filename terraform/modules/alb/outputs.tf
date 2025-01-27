output "alb_arn" {
  value = aws_lb.this.arn
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "alb_sg_id" {
  value = aws_security_group.alb_sg.id
}

output "listener_arn" {
  value = aws_lb_listener.https.arn
}
