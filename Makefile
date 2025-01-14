# TODO

.PHONY: terraform-init
terraform-init:
	@cd terraform && terraform init --backend-config ./state.config -lock=false

.PHONY: terraform-apply
terraform-apply:
	@cd terraform && terraform apply -lock=false

.PHONY: terraform-destroy
terraform-destroy:
	@cd terraform && terraform destroy

.PHONY: terraform-plan
terraform-plan:
	@cd terraform && terraform plan -lock=false


