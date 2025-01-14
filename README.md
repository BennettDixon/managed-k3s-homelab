Check back soon




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