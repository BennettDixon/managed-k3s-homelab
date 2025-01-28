# Single instance public proxy
This is a simple one instance public proxy using tailscale & NGINX to terminate TLS traffic and forward it to our tailnet. Primarily our private K3s cluster that runs on my homelab and is not exposed to the public internet directly.

This proxy will not scale well, but it is cheap and effective for my homelab setup. I have a work in progress high availability, horizontal version of the proxy that utilizes ECS Fargate + ALB, but it is a cost prohibitive option for my homelab.


## Prerequisites
1. Create a new tag for your proxy in tailscale:
```yaml
"tagOwners": {
    "tag:proxy": ["group:ops"],
}
```
2. Generate a tailscale auth key that is ephemeral and only used for this instance, tag the key with your new tag.
3. Update tailscale ACL to allow communication from your tag for the instance to your desired destinations, e.g:

```yaml
"acls": [
    // Allow proxy to talk to k8s public
    {
        "action":     "accept",
        "src":        ["tag:proxy"],
        "dst":        ["tag:k8s-public:*"],
        "srcPosture": ["posture:primaryStable"],
    },
    // ...
]
```
4. Update tailscale ACL to allow you ssh access to the instance via the tag:
```yaml
// ...
"ssh": [
    // Allow ops to ssh to proxy & k8s-public machines enabled w/ check
    {
        "action": "check",
        // Customize src as needed
        "src":    ["group:ops"],
        // Example dst(s)
        "dst":    ["tag:proxy", "tag:k8s-public"],
        // Customize users as needed
        "users":  ["ubuntu"],
    },
]
```

## Setup
1. Deploy lightsail instance with terraform

```bash
terraform init
terraform apply
```

2. Verify the static IP was created and assigned to the instance, if it was not re-run `terraform apply`

3. Update your domain DNS records to point to the static IP, wait for records to propegate

4. SSH into the instance over tailscale

```bash
ssh ubuntu@<tailscale-ip>
```

5. Verify DNS records have propegated on your lightsail instance:
```bash
dig <your-domain>
```

6. Run the certbot setup and input your info
```bash
certbot --nginx
```

7. Configure automatic renewal of certificates
```bash
echo "0 0,12 * * * root /opt/certbot/bin/python -c 'import random; import time; time.sleep(random.random() * 3600)' && sudo certbot renew -q" | sudo tee -a /etc/crontab > /dev/null
```
