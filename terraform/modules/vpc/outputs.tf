output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnets" {
  value = [for pub in aws_subnet.public : pub.id]
}

output "private_subnets" {
  value = [for priv in aws_subnet.private : priv.id]
}
