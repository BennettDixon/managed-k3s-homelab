# Single instance public proxy
This is a simple one instance public proxy using tailscale & NGINX to terminate TLS traffic and forward it to our tailnet. Primarily our private K3s cluster that runs on my homelab and is not exposed to the public internet directly.

This proxy will not scale well, but it is cheap and effective for my homelab setup. I have a work in progress high availability, horizontal version of the proxy that utilizes ECS Fargate + ALB, but it is a cost prohibitive option for my homelab.
