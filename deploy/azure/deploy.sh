#!/usr/bin/env bash
# Deploy the SAFE SIMULATOR demo of Aegis to Azure Container Apps.
#
# What this creates (all in one resource group, so one command deletes it):
#   - Azure Container Registry           image storage (built in the cloud, no local Docker needed)
#   - PostgreSQL Flexible Server         durable state, public access limited to Azure services
#   - Container Apps environment         with Log Analytics
#   - aegis-api     external HTTPS ingress, 1-2 replicas, /health liveness and /ready readiness probes
#   - aegis-worker  NO ingress, 1 replica
# Secrets are stored as Container Apps secrets, never in the image.
#
# The demo controls a simulator only. It has no permissions on any real infrastructure.
#
# Prerequisites: Azure CLI (az) logged in with `az login`, and the containerapp extension:
#   az extension add --name containerapp --upgrade
# Run from the repository root, in Git Bash, WSL, macOS, or Linux:
#   AEGIS_APPROVER_PASSWORD='choose-a-long-password' ./deploy/azure/deploy.sh
# Safe to rerun: it reuses the registry, database server, and environment it
# already created in the resource group instead of paying for duplicates.
# Remove everything afterwards (stops all costs):
#   az group delete --name "$RESOURCE_GROUP" --yes
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-aegis-demo-rg}"
LOCATION="${LOCATION:-northeurope}"

# On a rerun, reuse the resources an earlier run created (their names carry a random suffix).
existing() {  # existing <list command...>: name of the first resource in the group, or empty
  "$@" --resource-group "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true
}
if [ "$(az group exists --name "$RESOURCE_GROUP")" = "true" ]; then
  ACR_NAME="${ACR_NAME:-$(existing az acr list)}"
  PG_SERVER="${PG_SERVER:-$(existing az postgres flexible-server list)}"
fi
SUFFIX="${SUFFIX:-$(openssl rand -hex 3)}"
ACR_NAME="${ACR_NAME:-aegisacr${SUFFIX}}"
PG_SERVER="${PG_SERVER:-aegis-pg-${SUFFIX}}"
ENV_NAME="${ENV_NAME:-aegis-env}"
APPROVER_NAME="${AEGIS_APPROVER_NAME:-reviewer}"
: "${AEGIS_APPROVER_PASSWORD:?Set AEGIS_APPROVER_PASSWORD to the password reviewers will use}"

PG_PASSWORD="$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | head -c 32)"
SECRET_KEY="$(openssl rand -hex 32)"
WEBHOOK_SECRET="$(openssl rand -hex 32)"
METRICS_TOKEN="$(openssl rand -hex 24)"
APPROVER_HASH="sha256:$(printf '%s' "$AEGIS_APPROVER_PASSWORD" | openssl dgst -sha256 | awk '{print $NF}')"

echo "==> Resource group $RESOURCE_GROUP in $LOCATION"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Container registry $ACR_NAME and cloud image build"
if az acr show --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --output none 2>/dev/null; then
  echo "    reusing existing registry"
else
  az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic --admin-enabled true --output none
fi
az acr build --registry "$ACR_NAME" --image aegis:latest . --output none
ACR_SERVER="$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)"
ACR_USER="$(az acr credential show --name "$ACR_NAME" --query username -o tsv)"
ACR_PASS="$(az acr credential show --name "$ACR_NAME" --query 'passwords[0].value' -o tsv)"

echo "==> PostgreSQL Flexible Server $PG_SERVER (smallest burstable tier)"
if az postgres flexible-server show --resource-group "$RESOURCE_GROUP" --name "$PG_SERVER" --output none 2>/dev/null; then
  echo "    reusing existing server (setting a fresh admin password)"
  az postgres flexible-server update --resource-group "$RESOURCE_GROUP" --name "$PG_SERVER" \
    --admin-password "$PG_PASSWORD" --output none
else
  az postgres flexible-server create \
    --resource-group "$RESOURCE_GROUP" --name "$PG_SERVER" --location "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user aegis --admin-password "$PG_PASSWORD" \
    --public-access 0.0.0.0 --yes --output none   # 0.0.0.0 = allow Azure services only, not the internet
fi
# The database-name option was renamed to --name in newer Azure CLI versions; accept both.
if ! az postgres flexible-server db show --resource-group "$RESOURCE_GROUP" --server-name "$PG_SERVER" \
    --database-name aegis --output none 2>/dev/null \
  && ! az postgres flexible-server db show --resource-group "$RESOURCE_GROUP" --server-name "$PG_SERVER" \
    --name aegis --output none 2>/dev/null; then
  az postgres flexible-server db create --resource-group "$RESOURCE_GROUP" --server-name "$PG_SERVER" \
    --name aegis --output none 2>/dev/null \
  || az postgres flexible-server db create --resource-group "$RESOURCE_GROUP" --server-name "$PG_SERVER" \
    --database-name aegis --output none
fi
DATABASE_URL="postgresql+psycopg://aegis:${PG_PASSWORD}@${PG_SERVER}.postgres.database.azure.com:5432/aegis?sslmode=require"

echo "==> Container Apps environment $ENV_NAME"
if az containerapp env show --resource-group "$RESOURCE_GROUP" --name "$ENV_NAME" --output none 2>/dev/null; then
  echo "    reusing existing environment"
else
  az containerapp env create --resource-group "$RESOURCE_GROUP" --name "$ENV_NAME" --location "$LOCATION" --output none
fi

ENV_ID="$(az containerapp env show --resource-group "$RESOURCE_GROUP" --name "$ENV_NAME" --query id -o tsv)"
SPEC_DIR="$(mktemp -d)"
trap 'rm -rf "$SPEC_DIR"' EXIT   # the specs contain secrets: always delete them

# Writes a Container App spec (JSON is valid YAML for `az containerapp create --yaml`).
write_spec() {  # $1=app name  $2=role (api|worker)  $3=output file
  APP="$1" ROLE="$2" OUT="$3" LOCATION="$LOCATION" ENV_ID="$ENV_ID" IMAGE="$ACR_SERVER/aegis:latest" \
  ACR_SERVER="$ACR_SERVER" ACR_USER="$ACR_USER" ACR_PASS="$ACR_PASS" DATABASE_URL="$DATABASE_URL" \
  SECRET_KEY="$SECRET_KEY" WEBHOOK_SECRET="$WEBHOOK_SECRET" METRICS_TOKEN="$METRICS_TOKEN" \
  APPROVERS="${APPROVER_NAME}:${APPROVER_HASH}" python3 - <<'PY'
import json, os
e = os.environ
api = e["ROLE"] == "api"
secrets = {
    "acr-password": e["ACR_PASS"], "database-url": e["DATABASE_URL"], "secret-key": e["SECRET_KEY"],
    "webhook-secret": e["WEBHOOK_SECRET"], "metrics-token": e["METRICS_TOKEN"], "approvers": e["APPROVERS"],
}
env = [
    {"name": "AEGIS_ENV", "value": "production"},
    {"name": "AEGIS_LLM_PROVIDER", "value": "mock"},
    {"name": "DATABASE_URL", "secretRef": "database-url"},
    {"name": "AEGIS_SECRET_KEY", "secretRef": "secret-key"},
    {"name": "AEGIS_WEBHOOK_SECRET", "secretRef": "webhook-secret"},
    {"name": "AEGIS_METRICS_TOKEN", "secretRef": "metrics-token"},
    {"name": "AEGIS_USERS", "secretRef": "approvers"},
]
container = {
    "name": e["APP"],
    "image": e["IMAGE"],
    "env": env,
    "resources": {"cpu": 0.5, "memory": "1Gi"} if api else {"cpu": 0.25, "memory": "0.5Gi"},
}
config = {
    "secrets": [{"name": k, "value": v} for k, v in secrets.items()],
    "registries": [{"server": e["ACR_SERVER"], "username": e["ACR_USER"], "passwordSecretRef": "acr-password"}],
}
if api:
    # Container Apps' ingress is the only route in, so trusting its forwarded headers is safe.
    env += [{"name": "AEGIS_PROCESS_INLINE", "value": "true"}, {"name": "FORWARDED_ALLOW_IPS", "value": "*"}]
    config["ingress"] = {"external": True, "targetPort": 8000, "transport": "auto", "allowInsecure": False}
    container["probes"] = [
        {"type": "Liveness", "httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 15},
        {"type": "Readiness", "httpGet": {"path": "/ready", "port": 8000}, "periodSeconds": 10},
    ]
    scale = {"minReplicas": 1, "maxReplicas": 2}
else:
    env.append({"name": "WORKER_METRICS_PORT", "value": "9100"})
    container["command"] = ["python", "-m", "src.worker"]
    scale = {"minReplicas": 1, "maxReplicas": 1}
spec = {
    "location": e["LOCATION"],
    "properties": {
        "managedEnvironmentId": e["ENV_ID"],
        "configuration": config,
        "template": {"containers": [container], "scale": scale},
    },
}
with open(e["OUT"], "w") as f:
    json.dump(spec, f)
PY
}

create_or_update() {  # create_or_update <app name> <spec file>
  if az containerapp show --resource-group "$RESOURCE_GROUP" --name "$1" --output none 2>/dev/null; then
    az containerapp update --resource-group "$RESOURCE_GROUP" --name "$1" --yaml "$2" --output none
  else
    az containerapp create --resource-group "$RESOURCE_GROUP" --name "$1" --yaml "$2" --output none
  fi
}

echo "==> API app (external HTTPS, health probes)"
write_spec aegis-api api "$SPEC_DIR/api.yaml"
create_or_update aegis-api "$SPEC_DIR/api.yaml"

echo "==> Worker app (no ingress)"
write_spec aegis-worker worker "$SPEC_DIR/worker.yaml"
create_or_update aegis-worker "$SPEC_DIR/worker.yaml"

FQDN="$(az containerapp show --resource-group "$RESOURCE_GROUP" --name aegis-api --query properties.configuration.ingress.fqdn -o tsv)"
echo
echo "Deployed: https://${FQDN}"
echo "Approver login: ${APPROVER_NAME} / (the password you chose)"
echo "Webhook secret (keep private, needed to send alerts): ${WEBHOOK_SECRET}"
echo "Metrics token: ${METRICS_TOKEN}"
echo
echo "Smoke test:"
echo "  AEGIS_USER=${APPROVER_NAME} AEGIS_PASSWORD=... AEGIS_WEBHOOK_SECRET=${WEBHOOK_SECRET} AEGIS_METRICS_TOKEN=${METRICS_TOKEN} python scripts/smoke_test.py https://${FQDN}"
