# Public Proxy
The public proxy serves as an entrypoint for public access to my cluster over tailscale.

This readme is a work in progress.

The containers are deployed on ECS and scale up and down as needed; authenticating to the tailnet and routing traffic as defined in the `nginx.conf` file.