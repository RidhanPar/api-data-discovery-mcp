#!/usr/bin/env bash
# One-command teardown:  make azure-down
# Destroys everything Terraform created (one resource group) and purges the soft-deleted
# Key Vault and Azure OpenAI account, so nothing keeps costing money or blocks a redeploy.
set -euo pipefail
cd "$(dirname "$0")/../infra"
terraform destroy -input=false -auto-approve
echo "Destroyed. Check nothing is left: az group list --query \"[?starts_with(name, 'rg-')].name\" -o tsv"
