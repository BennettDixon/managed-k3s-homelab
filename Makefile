# TODO

.PHONY: terraform-init
terraform-init:
	@cd terraform && terraform init --backend-config ./state.config

.PHONY: terraform-apply
terraform-apply:
	@cd terraform && terraform apply

.PHONY: terraform-destroy
terraform-destroy:
	@cd terraform && terraform destroy

.PHONY: terraform-plan
terraform-plan:
	@cd terraform && terraform plan


.PHONY: flux-logs
flux-logs:
	@sudo flux logs --kube-config=/etc/rancher/k3s/k3s.yaml

.PHONY: flux-kustomizations
flux-kustomizations:
	@sudo flux get kustomizations -A --kubeconfig /etc/rancher/k3s/k3s.yaml

