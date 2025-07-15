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

  # Optional: script to run at first boot for installing Tailscale + Nginx
  # (This is a basic example; adjust to your needs.)
  user_data = <<-EOF
    #!/bin/bash
    apt-get update -y

    # Install Tailscale
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg \
      | gpg --dearmor -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list \
      | tee /etc/apt/sources.list.d/tailscale.list
    apt-get update -y
    apt-get install -y tailscale nginx python3 python3-venv libaugeas0
    # Setup virtual environment for certbot
    python3 -m venv /opt/certbot/
    # Install Upgrade pip
    /opt/certbot/bin/pip install --upgrade pip
    # Install certbot
    /opt/certbot/bin/pip install certbot certbot-nginx
    # Link certbot to /usr/bin
    ln -s /opt/certbot/bin/certbot /usr/bin/certbot

    # Start tailscaled and authenticate (example using inline auth key - not recommended for production)
    # In production, consider a more secure approach (retrieving from Secrets Manager, etc.).
    (tailscaled --tun=userspace-networking &)
    sleep 5
    tailscale up --authkey=${var.tailscale_auth_key} --ssh

    # Simple Nginx example
    cat <<NGINXCONF >/etc/nginx/sites-available/default
    # Root domain
    server {
        server_name www.bennettdixon.dev bennettdixon.dev;

        location / {
            proxy_pass http://personal-site-personal-site;  # Tailscale IP for personal site
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            # Astro requires this since we terminate SSL
            proxy_set_header Origin http://\$host;
        }
    }

    # Redirect www to root
    server {
        server_name www.bennettdixon.dev;
        # DELETE listen 80 for listen 443 once configured w/ ssl
        listen 80;

        # Add this manually or via certbot when configured
        # listen 443 ssl;
        # ssl_certificate /etc/letsencrypt/live/bennettdixon.dev/fullchain.pem;
        # ssl_certificate_key /etc/letsencrypt/live/bennettdixon.dev/privkey.pem;
        # include /etc/letsencrypt/options-ssl-nginx.conf;
        # ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

        return 301 https://bennettdixon.dev\$request_uri;
    }

    # FarmSim HTTP server (redirect to HTTPS)
    server {
        listen 80;
        server_name farmsim.bennettdixon.dev;
        return 301 https://\$server_name\$request_uri;
    }

    # FarmSim HTTPS server with SSL termination
    server {
        listen 443 ssl;
        server_name farmsim.bennettdixon.dev;

        # SSL certificate configuration (configure with certbot)
        # UNCOMMENT THIS BLOCK AFTER OBTAINING SSL CERTIFICATES
        # sudo certbot --nginx -d farmsim.bennettdixon.dev
        #ssl_certificate /etc/letsencrypt/live/farmsim.bennettdixon.dev/fullchain.pem;
        #ssl_certificate_key /etc/letsencrypt/live/farmsim.bennettdixon.dev/privkey.pem;
        #include /etc/letsencrypt/options-ssl-nginx.conf;
        #ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

        # Security headers
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        add_header X-Frame-Options DENY always;
        add_header X-Content-Type-Options nosniff always;
        add_header X-XSS-Protection "1; mode=block" always;

        location / {
            # Proxy to FarmSim backend over HTTP (internal Tailscale network)
            proxy_pass http://farmsim;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_set_header Origin http://\$host;
            
            # WebSocket support (in case FarmSim uses WebSockets)
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
            
            # Timeouts
            proxy_connect_timeout 60s;
            proxy_send_timeout 60s;
            proxy_read_timeout 60s;
        }
    }
    
    NGINXCONF

    # Add stream configuration for TCP proxying (game port)
    # Check if stream block already exists to avoid duplicates
    if ! grep -q "stream {" /etc/nginx/nginx.conf; then
        cat <<STREAMBLOCK >>/etc/nginx/nginx.conf

# Stream block for TCP/UDP proxying
stream {
    # FarmSim game server TCP proxy
    server {
        listen 10823;
        proxy_pass farmsim:10823;
        proxy_timeout 1s;
        proxy_responses 1;
    }
}

STREAMBLOCK
    fi

    systemctl restart nginx

    # BE SURE TO CONFIGURE THE SSL CERTS WITH CERTBOT VIA SSH
    # ONCE SETUP OF VM IS COMPLETE
    # (SEE README FOR INSTRUCTIONS)
  EOF
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