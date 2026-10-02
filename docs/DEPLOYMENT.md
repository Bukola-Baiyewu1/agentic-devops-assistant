# Deploying the safe simulator demo to Azure Container Apps

The deployed demo controls only the built-in simulator. It has no permissions on
any real infrastructure.

## What gets created

| Resource | Purpose | Exposure |
|---|---|---|
| Resource group `aegis-demo-rg` | Holds everything; deleting it removes all costs | n/a |
| Azure Container Registry (Basic) | Stores the image, built in the cloud by `az acr build` | private |
| PostgreSQL Flexible Server (Burstable B1ms) | Durable state | Azure services only |
| Container Apps environment | Hosting and Log Analytics | n/a |
| `aegis-api` | API and approval UI, 1-2 replicas, `/health` liveness and `/ready` readiness probes | public, HTTPS only |
| `aegis-worker` | Retries, dead letters, expiry | no ingress |

All secrets (database URL, signing key, webhook secret, metrics token, approver
hash) are stored as Container Apps secrets and referenced by environment
variables. None are in the image or the repository.

## Steps

1. Install the Azure CLI and sign in:
   ```bash
   az login
   az extension add --name containerapp --upgrade
   ```
2. From the repository root (Git Bash, WSL, macOS, or Linux):
   ```bash
   AEGIS_APPROVER_PASSWORD='a-long-password-you-choose' ./deploy/azure/deploy.sh
   ```
   The script prints the HTTPS URL, the webhook secret, and the metrics token.
3. Run the remote smoke test with the printed values:
   ```bash
   AEGIS_USER=reviewer AEGIS_PASSWORD='...' AEGIS_WEBHOOK_SECRET='...' AEGIS_METRICS_TOKEN='...' \
     python scripts/smoke_test.py https://<your-app>.azurecontainerapps.io
   ```
4. When you are done:
   ```bash
   az group delete --name aegis-demo-rg --yes
   ```

## Settings worth changing

| Variable | Default | Notes |
|---|---|---|
| `RESOURCE_GROUP` | `aegis-demo-rg` | |
| `LOCATION` | `northeurope` | Any region offering Container Apps and PostgreSQL Flexible Server |
| `AEGIS_APPROVER_NAME` | `reviewer` | Login name for approvers |

The deployed API runs the mock planner (`AEGIS_LLM_PROVIDER=mock`) so a public
demo never spends model credits. To use Claude, add the key as a secret:

```bash
az containerapp secret set -g aegis-demo-rg -n aegis-api --secrets anthropic-key=<key>
az containerapp update -g aegis-demo-rg -n aegis-api \
  --set-env-vars ANTHROPIC_API_KEY=secretref:anthropic-key AEGIS_LLM_PROVIDER=anthropic AGENT_MODEL=<model-id>
```

Do the same for the worker, which also plans when it retries events.

## Cost control

The smallest tiers are used and the API scales to at most two replicas. Set a
budget alert on the subscription, and delete the resource group when the demo
is not needed.
