# TODO

# PHONY: terraform-init
terraform-init:
	@cd terraform && terraform init --backend-config ./state.config -lock=false