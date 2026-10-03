#!/usr/bin/env bash
# One-command Azure deployment:  make azure-up
#
# Needs: az (logged in: `az login`), terraform >= 1.9, docker, and infra/terraform.tfvars
# (copy infra/terraform.tfvars.example). Idempotent: re-run to roll out a new commit.
#   1. registry first (images must exist before the apps are created)
#   2. build + push the three images, tagged with the git commit
#   3. everything else
#   4. run the migrations + catalog ingestion job and wait for it
set -euo pipefail
cd "$(dirname "$0")/../infra"
[ -f terraform.tfvars ] || { echo "infra/terraform.tfvars missing: cp infra/terraform.tfvars.example infra/terraform.tfvars"; exit 1; }
TAG=$(git rev-parse --short=12 HEAD)
git diff --quiet HEAD -- ../src ../deploy ../catalog ../Dockerfile || echo "warning: uncommitted changes are deployed under tag $TAG"

terraform init -input=false
terraform apply -input=false -auto-approve -target=azurerm_container_registry.main
ACR=$(terraform output -raw acr_name)
az acr login --name "$ACR"
REG="$ACR.azurecr.io"
docker build --platform linux/amd64 -t "$REG/nordlys-discovery:$TAG" ..
docker build --platform linux/amd64 -t "$REG/nordlys-keycloak:$TAG" ../deploy/keycloak
docker build --platform linux/amd64 -t "$REG/nordlys-n8n:$TAG" ../deploy/n8n
for img in nordlys-discovery nordlys-keycloak nordlys-n8n; do docker push "$REG/$img:$TAG"; done

terraform apply -input=false -auto-approve -var "image_tag=$TAG"
RG=$(terraform output -raw resource_group)

echo "Running migrations + ingestion..."
EXEC=$(az containerapp job start -g "$RG" -n ingest --query name -o tsv)
for _ in $(seq 1 90); do
  STATUS=$(az containerapp job execution show -g "$RG" -n ingest --job-execution-name "$EXEC" --query properties.status -o tsv)
  case "$STATUS" in
    Succeeded) echo "ingestion done"; break ;;
    Failed) echo "ingestion failed: az containerapp job logs show -g $RG -n ingest --execution $EXEC"; exit 1 ;;
    *) sleep 10 ;;
  esac
done

echo
terraform output
echo "Demo user passwords are in Key Vault $(terraform output -raw key_vault) (demo-password-alice, ...)."
