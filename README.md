## K8s Flux Stack
An opinionated starting point for a cloud-native based K8s cluster with [fluxcd](https://fluxcd.io) for automated cluster management.

I run this on my homelab on [K3s](https://k3s.io), but it could be used on any k8s cluster. I also utilize AWS for my terraform state storage & secret manager, but you can use whatever you like. You will just need to update terraform for your new provider, and update the external secrets operator (ESO) in your cluster to use your own secret manager.

## Uses
- [fluxcd](https://fluxcd.io) - GitOps for Kubernetes
- [Kustomize](https://kustomize.io/) & [Flux Kustomize Controller](https://github.com/fluxcd/kustomize-controller)
- [Harbor](https://goharbor.io) - Artifact Registry with features like Vulnerability Scanning with Trivy, RBAC, and SSO
- [cert-manager](https://cert-manager.io) - TLS Certificate Management
- [external-secrets.io](https://external-secrets.io) - ESO for k8s
- [terraform](https://www.terraform.io) for cloud infrastructure
- AWS Secrets Manager (external secrets store for k8s)
- AWS S3 (Terraform State Store)

