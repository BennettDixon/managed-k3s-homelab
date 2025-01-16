Hey, these are my notes for setting up the README for this repo.


### Setup
Just notes for now

```bash
mv terraform/terraform.tfvars.empty terraform/terraform.tfvars
```

```bash
mv terraform/state.config.empty terraform/state.config
```

Configure your S3 backend for terraform manually with the desired folder for your state storage, then update your `terraform/state.config` file with the appropriate values.

Finally run `make terraform-init` to initialize terraform with the proper AWS modules and state files.


## Setup flux on k3s cluster
https://fluxcd.io/flux/installation/
`curl -s https://fluxcd.io/install.sh | sudo bash`
`sudo flux bootstrap github  --owner=bennettdixon  --repository=k3s-homelab  --branch=main  --path=cluster --personal --kubeconfig /etc/rancher/k3s/k3s.yaml`


Manually create AWS IAM user with access permissions for reading secrets and add secret to k3s cluster after the `external-secrets` namespace has been created

```bash
sudo kubectl create secret generic aws-creds -n external-secrets   --from-literal=aws_access_key_id=YOUR_AWS_ACCESS_KEY_ID  --from-literal=aws_secret_access_key=YOUR_AWS_SECRET_ACCESS_KEY --kubeconfig=/etc/rancher/k3s/k3s.yaml
```


Add an entry to your DNS server to allow access to your harbor ingress controller
```
$ sudo kubectl get ingress -n harbor
NAME             CLASS     HOSTS             ADDRESS        PORTS     AGE
harbor-ingress   traefik   harbor.internal   <your-local-ip>   80, 443   17m
```

Then add an entry to your DNS server:
```yaml
Host: harbor
Domain: internal
Type: A
Value: <your-local-ip>
Description: Harbor Server
```
Create a non root user for harbor as the main admin. Create other users as needed. Create a user for docker access that is a maintainer.

Create a project in harbor, and download the certificate.

Add the registry certificates to your local docker daemon

https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/

Login to docker with your local registry: `docker login harbor.internal`
