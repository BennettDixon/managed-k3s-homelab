#!/usr/bin/env bash
set -e

# Start tailscaled in the background
/usr/sbin/tailscaled --tun=userspace-networking &

# Give tailscaled a few seconds to start up
sleep 5

# Bring up Tailscale. You must provide TAILSCALE_AUTH_KEY (and optionally TAILSCALE_HOSTNAME).
# For ephemeral keys, add --ephemeral to the tailscale up command below.
tailscale up --authkey="${TAILSCALE_AUTH_KEY}" 

echo "Tailscale running, starting Nginx in 10 seconds"
sleep 10

# Start Nginx in the foreground
exec nginx -g "daemon off;"

# Hang for debug
#tail -f /dev/null
