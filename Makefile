.PHONY: terraform-init
terraform-init:
	@cd terraform && terraform init --backend-config ./state.config

.PHONY: show-grafana-login
show-grafana-login:
	@echo "Username: "
	kubectl get secret -n kube-prometheus-stack kube-prometheus-stack-grafana -o jsonpath='{.data.admin-user}' | base64 -d
	@echo "\nPassword: "
	kubectl get secret -n kube-prometheus-stack kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d
	@echo "\n"

.PHONY: setup-terraform-templates
setup-terraform-templates:
	@./scripts/setup-terraform-templates