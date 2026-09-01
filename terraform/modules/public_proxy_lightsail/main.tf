# This is a single instance light-weight proxy for use in the homelab
# for a highly available option you may want to condsider using the
# /public-proxy with a ECS Fargate + ALB setup.

# For this to work you must create it, get the static IP, and then
# associate the ip with your domain DNS
# Once it is available from the provider you can use certbot to get
# the SSL certificate and restart nginx with the valid certificates.


########################
# Create a Lightsail VM
########################
resource "aws_lightsail_instance" "tailscale_proxy" {
  name              = var.ls_instance_name
  availability_zone = var.ls_availability_zone  # e.g. "us-east-1a"
  blueprint_id      = "ubuntu_22_04"            # or "ubuntu_20_04", "amazon_linux_2", etc.
  bundle_id         = "nano_2_0"                # e.g. "nano_2_0", "micro_2_0", etc.

  # First-boot provisioning: installs tailscale + nginx + certbot and runs
  # both daemons under systemd (restart-on-failure) so crashes self-heal.
  # NOTE: user_data only runs at first boot, and changing it forces
  # replacement of the instance (see README ops notes before applying).
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    tailscale_auth_key = var.tailscale_auth_key
  })
}

############################
# Allocate and Attach a Static IP
############################
resource "aws_lightsail_static_ip" "proxy_ip" {
  name = "${var.ls_instance_name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "proxy_ip_attach" {
  static_ip_name = aws_lightsail_static_ip.proxy_ip.name
  instance_name  = aws_lightsail_instance.tailscale_proxy.name
}

########################################
# Open Inbound Ports (80, 443) in Lightsail
########################################
resource "aws_lightsail_instance_public_ports" "proxy_ports" {
  instance_name = aws_lightsail_instance.tailscale_proxy.name

  port_info {
    from_port = 80
    to_port   = 80
    protocol  = "tcp"
  }
  port_info {
    from_port = 443
    to_port   = 443
    protocol  = "tcp"
  }
  port_info {
    from_port = 10823
    to_port   = 10823
    protocol  = "tcp"
  }
}