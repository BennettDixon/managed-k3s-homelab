# Public Proxy
This is the configuration for the highly available public proxy.

I have not finished this implementation as it was cost prohibitive for my homelab.

I instead use a simple lightsail vm instance with a simple nginx + tailscale setup - found in `/terraform/modules/public_proxy_lightsail`.