## K8s Flux Stack
A starter template for deploying a Kubernetes cluster using Flux GitOps, integrated with essential tools like Harbor (container registry), Prometheus & Grafana (monitoring and visualization), and Terraform for managing secrets on AWS. This repository provides a structured approach to managing your Kubernetes infrastructure and applications declaratively through Git.


### Features
- **Flux GitOps:** Continuous deployment and synchronization with Git.
- **Harbor:** Secure and efficient container image registry.
- **Prometheus & Grafana:** Robust monitoring and visualization stack.
- **Terraform with AWS:** Infrastructure as Code for managing secrets and AWS resources.
- **Modular Configuration:** Organized manifests and configurations for scalability.
- **Dependency Management:** Ensures the correct order of resource deployment using Flux Kustomizations.

### Repository Structure
```perl
k8s-flux-starter/
├── apps/
|   ├── base/                           # Base applications manifests
|   |   ├── harbor/                     # Harbor deployment manifests
|   |   ├── kube-prometheus-stack/      # Monitoring via Prometheus & Grafana
│   ├── production/                     # Production applications manifests
│   └── secrets/                        # Application secrets manifests
├── clusters/
│   └── production/                     # High-level cluster configuration for Flux
├── infrastructure/
│   ├── controllers/                    # Infrastructure controllers manifests
│   ├── configs/                        # Infrastructure configurations manifests
│   └── network/                        # Network configurations manifests
├── terraform/
│   |   ├── modules/                    # Terraform modules
│   └── main.tf                         # Terraform main configuration
|   └── state.tf                        # Terraform state configuration
|   └── provider.tf                     # Terraform provider configuration
|   └── variables.tf                    # Terraform variables
|   └── terraform.tfvars.empty          # Terraform variables placeholder (sensitive once moved)
|   └── state.config.empty              # Terraform state configuration placeholder (sensitive once moved)
├── README.md       
└── ...                                 # Additional configurations and scripts

```

### Prerequisites
1. Kubernetes Cluster >= 1.29 - if not using K3s you will need to customize the ingress controller.
  - **Local Cluster:** K3s, Kind, Minikube, etc.
  - **Managed Cluster:** AWS EKS, GKE, AKS, etc.
2. Flux CLI
Flux CLI will be used to bootstrap flux on the cluster. Ensure FluxCLI is installed on your cluster machine or wherever you apply kubectl commands. Refer to the Flux [CLI Installation Guide](https://fluxcd.io/flux/installation/#install-the-flux-cli).
3. Terraform
Ensure Terraform is installed on your development machine. Follow the [Terraform Installation Guide](https://developer.hashicorp.com/terraform/tutorials/aws-get-started/install-cli) for your preferred installation method.
4. AWS CLI
We will be utilize AWS as our state backend for Terraform, as well as our secrets manager. Ensure AWS CLI is installed on your development machine for use with Terraform. If you would like to use a different cloud provider you can customize the Terraform configuration. Refer to the [AWS CLI Installation Guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) for your preferred installation method.

Ensure AWS CLI is configured with your access tokens; I recommend creating a dedicated IAM user for Terraform.
```bash
aws configure
```
5. Make (optional)
This repository uses Make to manage the terraform and flux setup. Technically it is not required, but it will make setup a bit easier. Refer to the [CMake Installation Guide](https://cmake.org/download/) for your preferred installation method.

### Setup Guide
Follow these steps to set up your Kubernetes cluster using the **k8s-flux-starter** repository.

#### 1. Create your copy of the template
Create a copy of this template repository if you have not already, or fork it. Once you have done so be sure to pull the repository to your development machine.


#### 2. Create a state backend bucket for Terraform
Manually create an S3 bucket for Terraform state storage. This bucket will be used for storing the Terraform state file. I would recommend using a versioned bucket to ensure there is a git-like history of the state file, but this is not required.

Finally create a directory in the bucket we can use to store our state file, e.g 'state'.

#### 3 Update template tfvars & stateconfig
The repository ships with an empty template for `terraform/terraform.tfvars` and `terraform/state.config`. We need to setup the actual files with our values.

1. Copy our empty template files to the actual files which will be ignored by our `.gitignore`:
```bash
make setup-terraform-templates
# OR if you don't have Make
./scripts/setup-terraform-templates.sh
```
2. Update the `terraform.tfvars` and `state.config` files with your desired values.
```bash
# state.config
bucket  = "" # The name of the bucket you manually created to store the state
key     = "" # The key of the state file in the bucket, the prefix needs to be present
# E.g: state/terraform.tfstate => state directory must be present in bucket, but not the state file
region  = "" # The AWS Region you created the bucket in
profile = "" # The AWS profile you configured to use with aws cli (typically 'default')
             # Can also be found in ~/.aws/credentials
```
```bash
# terraform.tfvars
aws_region = "" # Your preferred AWS region (currently used for secret manager)
harbor_admin_password = "" # Whatever password you wish to use for the harbor admin account
```


#### 4. Configure AWS with Terraform
Navigate to the Terraform directory and initialize the workspace.
```bash
cd terraform
terraform init
```

#### 5. Provision AWS Resources
Apply the Terraform configuration to create necessary AWS resources for secrets management.
```bash
terraform apply
```
- **Review the plan** and confirm the creation by typing yes when prompted.
- **Outputs:** Note any output values, such as secret ARNs or access details, needed for further configuration.

#### 6. Setup Kubernetes Cluster
Setup a simple K3s cluster for testing the app, setup K3s or Rancher Desktop. Alternatively you can use an existing cluster, but you may need to tweak the ingress controller if you don't use K3s.

#### 7. Install Flux
Bootstrap Flux to watch the repository and manage the cluster configuration.
```bash
flux bootstrap github \
  --owner=<your-github-username> \
  --repository=<your-repository-name> \
  --branch=main \
  --path=./clusters/development \
  --personal
```
- **Parameters:**
  - `--owner`: Your GitHub username or organization.
  - `--repository`: Name of your repository (k8s-flux-starter).
  - `--path`: Directory containing Flux configurations (./clusters/my-cluster).
  - `--personal`: Use this flag if it's a personal repository.
This command sets up Flux controllers in your cluster and configures them to sync with the specified GitHub repository path.

#### 8. Deploy Infrastructure Components
Deploy foundational infrastructure components such as controllers and configurations.

1. **Navigate to the Cluster Configuration Directory:**

```bash
cd ../clusters/development
```
2. **Ensure Infrastructure Kustomizations Are Defined:**

The `/clusters/development` & `/clusters/production` directory contains Kustomization manifests that define the order of deployment using dependencies.

Flux will automatically detect and apply these Kustomizations in the correct order.

3. Apply changes
Make any changes that you desire, and push the new changes to GitHub.
```bash
git add .
git commit -m "Update infrastructure"
git push origin main
```

4. Wait for Flux to reconcile the changes
Flux will automatically reconcile the changes and deploy the infrastructure components based on our interval settings. Alternatively we can manually trigger a reconciliation for the system manually on our cluster:
```bash
flux reconcile ks flux-system --with-source
```

5. Monitor the progress
We can view the progress of the reconciliation by checking the logs in the Flux system namespace:
```bash
kubectl logs deployments/helm-controller -n flux-system
kubectl logs deployments/kustomize-controller -n flux-system
kubectl logs deployments/source-controller -n flux-system
kubectl logs deployments/notification-controller -n flux-system
```
The `kustomize-controller` is typically where you want to look during a reconciliation, but the other controllers are also involved. For example if there is an update to a HelmRepository or HelmRelase the `helm-controller` would be the one to check.

6. Verify Infrastructure
Ensure everything is still running as expected:
```bash
flux get kustomizations -A
```
Should display that all kustomizations are up to date and ready.

```bash
kubectl get all -n flux-system
```
Should display all the resources that Flux has deployed in a consistent state.

## Uses
- [fluxcd](https://fluxcd.io) - GitOps for Kubernetes
- [Kustomize](https://kustomize.io/) & [Flux Kustomize Controller](https://github.com/fluxcd/kustomize-controller)
- [Harbor](https://goharbor.io) - Artifact Registry with features like Vulnerability Scanning with Trivy, RBAC, and SSO
- [Kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) - Prometheus & Grafana helm chart
  - [Prometheus](https://prometheus.io) - Monitoring
  - [Grafana](https://grafana.com) - Monitoring
- [cert-manager](https://cert-manager.io) - TLS Certificate Management
- [external-secrets.io](https://external-secrets.io) - ESO for k8s
- [terraform](https://www.terraform.io) for cloud infrastructure
- AWS Secrets Manager (external secrets store for k8s)
- AWS S3 (Terraform State Store)



## Roadmap
- Improve documentation
- Implement RBAC & Network policies to restrict cross-namespace access
- Implement testing
- Add sample apps
- Add a unified logging layer using Fluentd