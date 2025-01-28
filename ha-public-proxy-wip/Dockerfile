FROM ubuntu:22.04

# Install dependencies: curl for adding Tailscale repo, tailscale, and nginx
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl gnupg2 iproute2 dnsutils nginx ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Add the Tailscale APT repo
RUN curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && \
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list | tee /etc/apt/sources.list.d/tailscale.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends tailscale && \
    rm -rf /var/lib/apt/lists/*

# Copy Nginx config
COPY nginx.conf /etc/nginx/nginx.conf

# Create an entrypoint script to start tailscaled in the background,
# then bring up Tailscale, then run Nginx in the foreground.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose Nginx ports required
EXPOSE 80
EXPOSE 443

ENTRYPOINT ["/entrypoint.sh"]
