Hi, I'm building something! Check back soon :)

## Uses
- Terraform
- K3s by Rancher (lightweight Kubernetes)
- Flux (GitOps)
- Kustomize & Flux Kustomize Controller
- external-secrets.io (external secrets for k8s)
- AWS Secrets Manager (external secrets store for k8s)
- AWS S3 (Terraform State Store)
- Harbor (Artifact Registry)


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
