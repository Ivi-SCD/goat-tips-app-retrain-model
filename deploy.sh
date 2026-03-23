#!/usr/bin/env bash
# ============================================================
# Scout — Deploy Retrain Job to Azure Container Apps
# ============================================================
# Prerequisites:
#   az login
#   az extension add --name containerapp
#
# Run once to provision, then the cron handles weekly retraining.
# ============================================================
set -euo pipefail

# ── Config — edit these ──────────────────────────────────────
RESOURCE_GROUP="goat-tips"
LOCATION="westeurope"
ACR_NAME="goatipsacr"                    # must be globally unique
IMAGE_NAME="goattips-retrain"
IMAGE_TAG="latest"
ENVIRONMENT="goat-tips-env"
JOB_NAME="goattips-retrain-weekly"

# Secrets — set via environment or replace inline
SUPABASE_DB_URL="${SUPABASE_DB_URL:?SUPABASE_DB_URL required}"
AZURE_STORAGE_CONNECTION_STRING="${AZURE_STORAGE_CONNECTION_STRING:?AZURE_STORAGE_CONNECTION_STRING required}"
# ─────────────────────────────────────────────────────────────

echo "→ Creating resource group …"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "→ Creating Azure Container Registry …"
az acr create \
  --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --sku Basic \
  --admin-enabled true \
  --output none

echo "→ Building and pushing image …"
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${IMAGE_TAG}" \
  --file Dockerfile \
  .

ACR_SERVER="${ACR_NAME}.azurecr.io"
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

echo "→ Creating Container Apps environment …"
az containerapp env create \
  --name "$ENVIRONMENT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

echo "→ Creating Container Apps Job (weekly cron) …"
az containerapp job create \
  --name "$JOB_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --environment "$ENVIRONMENT" \
  --trigger-type "Schedule" \
  --cron-expression "0 3 * * 1" \
  --replica-timeout 600 \
  --replica-retry-limit 1 \
  --replica-completion-count 1 \
  --parallelism 1 \
  --image "${ACR_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --cpu 0.5 \
  --memory 1Gi \
  --env-vars \
    "SUPABASE_DB_URL=${SUPABASE_DB_URL}" \
    "AZURE_STORAGE_CONNECTION_STRING=${AZURE_STORAGE_CONNECTION_STRING}" \
    "AZURE_STORAGE_CONTAINER=models" \
    "MODEL_BLOB_NAME=poisson_model.pkl"

echo ""
echo "✓  Job '${JOB_NAME}' created — runs every Monday at 03:00 UTC"
echo ""
echo "Manual trigger:"
echo "  az containerapp job start --name ${JOB_NAME} --resource-group ${RESOURCE_GROUP}"
echo ""
echo "View logs:"
echo "  az containerapp job execution list --name ${JOB_NAME} --resource-group ${RESOURCE_GROUP} -o table"
