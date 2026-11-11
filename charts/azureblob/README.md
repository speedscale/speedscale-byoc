# Speedscale BYOC — Azure Blob Storage data-lake

Speedscale captures inbound + outbound traffic in the cluster and ships
RRPair logs through an OpenTelemetry Collector to an **Azure Blob Storage**
container. Use this scenario when you want a durable object-storage archive of
every observed request/response — for compliance retention, downstream
replay/training pipelines, or batch analytics — instead of (or in addition to)
a live query backend like Elasticsearch or Loki.

The collector's native `azure_blob` exporter writes blobs directly to Azure. A persistent in-cluster queue buffers delivery retries; the archive lives in the Blob container.

## proxymock pull support

Native reads use `proxymock import azureblob`, the `pull_byoc_bucket` MCP tool with `storage-provider=azureblob`, or **Azure Blob Storage** in the `proxymock web` traffic source picker. The client queries Azure directly and imports matching traffic into local RRPair files.

**Version requirement:** native Azure reads are available in **proxymock v2.5.1169 and later**. Older clients can upgrade or use [`scripts/azure-gather.py`](../../scripts/azure-gather.py) for manual retrieval. Installing this collector chart alone does not upgrade the proxymock client.

Authenticate on the machine running proxymock with `az login`, an Azure workload identity, or environment credentials supported by [DefaultAzureCredential](https://learn.microsoft.com/en-us/azure/developer/go/sdk/authentication/credential-chains). Grant the reader **Storage Blob Data Reader** on the container. Keep the collector's writer credentials separate.

```sh
az login
proxymock import azureblob \
  --azure-account-url https://mystorageacct.blob.core.windows.net \
  --bucket byoc --from now-1h --to now --service checkout \
  --limit 100 --out proxymock/azure-traffic --output json
```

`--bucket` names the Azure container. This chart writes `YYYY/MM/DD/logs_*` at the container root, so leave `--prefix` unset unless you configured a matching prefix in the exporter. The S3 chart's `byoc/` prefix does not apply. Existing time windows, traffic filters, DLP, limits, output formats, and `--follow` apply. A limited viewing sample can omit part of a journey; check completeness before replay.

For connection-string authentication, set `AZURE_STORAGE_CONNECTION_STRING` in the shell before launching proxymock and omit `--azure-account-url`. Store it outside command arguments, Helm values, and source control. Identity authentication uses `--azure-account-url` or `AZURE_STORAGE_ACCOUNT_URL`. Account-URL identity and connection-string settings are mutually exclusive. The URL must be an HTTPS account endpoint without a container path or SAS token.

In `proxymock web`, select **Azure Blob Storage**, enter the container and account URL, and use local Azure credentials. Leave the account URL empty when using a connection string. Azure cluster reader discovery is not supported; credentials come from the machine running proxymock.

For MCP, call `pull_byoc_bucket` with `storage-provider: azureblob`, `bucket: byoc`, and `azure-account-url: https://mystorageacct.blob.core.windows.net`. Credentials remain in the MCP server environment.

## Architecture

```mermaid
flowchart LR
  subgraph Yours[Your infrastructure]
    subgraph K8s[Kubernetes cluster<br/>GKE, EKS, AKS, or self-managed]
      Apps[Application pods]
      Cap[eBPF nettap / sidecar capture]
      Fwd[Speedscale Forwarder<br/>DLP + filter rules]
      OTel[OTel Collector<br/>OTLP gRPC :4317<br/>azureblob exporter]
    end
    ABS[(Azure Blob container<br/>your Azure subscription<br/>OTLP-JSON blobs)]
  end
  SC[Speedscale Cloud<br/>app.speedscale.com]

  Apps --> Cap --> Fwd
  Fwd -->|OTLP gRPC| OTel
  OTel -->|PutBlob| ABS
  Fwd -.->|control plane only:<br/>registration, DLP config| SC
```

This collector channel sends RRPairs to your Azure Blob container. Forwarder exporters are independent: enabling this channel does not disable the Speedscale Cloud exporter or other destinations. The diagram shows the Azure-only capture path after Cloud traffic export has been disabled. Before capture, verify every enabled exporter and its filter/DLP settings using the [data-boundary setup guide](https://docs.speedscale.com/byoc/#before-you-capture).

`charts/grafana/` and `charts/elasticsearch/` (sibling scenarios) are live-query
backends — you see traffic in a dashboard. This scenario is the **archive**
path: data lands as OTLP-JSON blobs in Azure Blob Storage, ready for cheap
long-term retention and batch consumption. It mirrors the `fluentbit-gcs` and
`fluentbit-s3` object-store charts, swapping the `awss3` exporter for
`azureblob`.

## Why pick this over `grafana` or `elasticsearch`?

Pick `azureblob` when:

- You need **durable retention** of every RRPair (compliance, audit, forensic replay) without paying ES/Loki hot-storage prices.
- You want to **feed downstream pipelines** — analytics, ML training corpora, proxymock snapshot generation — from a single canonical blob corpus.
- You want **lifecycle management** (auto-tier to Cool/Archive) that Azure Blob Storage handles natively.

Pick `grafana` or `elasticsearch` when you need an interactive dashboard / live query UI.

The scenarios coexist in their own namespaces; flip the forwarder's
`byoc_azure.otel_endpoint` to switch which one receives traffic.

> **Collector image:** this chart pins OpenTelemetry Collector Contrib `0.160.0` by digest in `values.yaml`. The `azure_blob` exporter is included in that image. Keep the pinned image for the supported reference configuration; validate custom image overrides before deployment.

## Prerequisites

1. **Azure Storage account** in a subscription you control:
   ```bash
   az storage account create \
     --name mystorageacct \
     --resource-group my-rg \
     --location eastus \
     --sku Standard_LRS
   ```

2. **Blob container** to hold the RRPairs:
   ```bash
   az storage container create \
     --name byoc \
     --account-name mystorageacct
   ```

3. **Connection string** for the storage account:
   ```bash
   az storage account show-connection-string \
     --name mystorageacct \
     --resource-group my-rg \
     --query connectionString -o tsv
   # DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net
   ```

4. **Kubernetes Secret** with the connection string (chart references it; does NOT manage it):
   ```bash
   kubectl create namespace byoc-azureblob
   kubectl -n byoc-azureblob create secret generic byoc-azureblob \
     --from-literal=connectionString='DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net'
   ```

## Install

```bash
helm repo add speedscale https://speedscale.github.io/operator-helm/
helm repo add speedscale-byoc https://speedscale.github.io/speedscale-byoc/
helm repo update

# Speedscale Operator + Forwarder
helm upgrade --install speedscale-operator speedscale/speedscale-operator \
  -n speedscale --create-namespace \
  --set apiKeySecret=speedscale-apikey \
  --set clusterName=<YOUR_CLUSTER_NAME> \
  --set 'forwarder.exporters.byoc_azure.otel_endpoint=http://otel-collector.byoc-azureblob.svc.cluster.local:4317' \
  --set 'forwarder.exporters.byoc_azure.filter_rule=standard' \
  --set 'forwarder.exporters.byoc_azure.dlp_config_id=standard'

# OTel Collector → Azure Blob Storage
helm upgrade --install byoc-azureblob speedscale-byoc/azureblob \
  -n byoc-azureblob --create-namespace \
  --set azure.url=https://mystorageacct.blob.core.windows.net/ \
  --set azure.container=byoc
```

Annotate a workload to capture its traffic:

```bash
kubectl patch deployment my-app -p '{"spec":{"template":{"metadata":{"annotations":{"capture.speedscale.com/enabled":"true"}}}}}'
```

## Verify

**1. Forwarder is wired**

```bash
kubectl -n speedscale get cm speedscale-forwarder \
  -o jsonpath='{.data.EXPORTERS}' | jq .
```

Expected: JSON with `byoc_azure` and `otel_endpoint` pointing at `byoc-azureblob`.

**2. OTel Collector is receiving logs**

```bash
kubectl -n byoc-azureblob logs deploy/otel-collector --tail=50 | grep -E 'LogsExporter|log_records'
```

Non-zero `log_records` = Forwarder is delivering. Zero = check endpoint and port.

**3. Collector is exporting to Azure Blob**

```bash
kubectl -n byoc-azureblob logs deploy/otel-collector --tail=50 | grep -iE 'azureblob|export|error'
```

Successful exports log without errors. `authentication`/`connection string`
errors point at the Secret; `403` points at the container or account key.

**4. Blobs appear in the container**

```bash
az storage blob list \
  --account-name mystorageacct \
  --container-name byoc \
  --auth-mode login \
  --output table | tail -10
```

New blobs appear within ~30s of traffic flowing. These reader commands require Storage Blob Data Reader on the container; the collector's ability to write does not grant access to your local identity.

**5. Peek at a record**

```bash
az storage blob download \
  --account-name mystorageacct \
  --container-name byoc \
  --auth-mode login \
  --name <blob-name> --file - 2>/dev/null | jq '.resourceLogs[0]' | head
```

## Troubleshoot

**`EXPORTERS` is null or missing `byoc_azure`**

Values weren't applied. Ensure you passed `forwarder.exporters.byoc_azure.*` on `helm upgrade`, then restart: `kubectl -n speedscale rollout restart deploy/speedscale-forwarder`.

**OTel Collector not receiving records**

- Port must be **4317** (gRPC). `4318` is the HTTP port — wrong for the Forwarder's gRPC dial.
- Namespace in the endpoint must match where you installed the chart.

**`http://` prefix required on `otel_endpoint`**

Always use `http://otel-collector.<namespace>.svc.cluster.local:4317`, not a bare `host:port`.

**Unknown exporter `azureblob` / collector crashloops on startup**

Use the digest-pinned OpenTelemetry Collector Contrib `0.160.0` image from `values.yaml`. A custom collector build or older image can omit the `azure_blob` exporter. The chart's earlier 1.0.0 release used 0.153.0; it is not the current default.

**Authentication errors**

The exporter authenticates from the connection string. Confirm the Secret
`byoc-azureblob` has a `connectionString` key whose value is the full
`DefaultEndpointsProtocol=...;AccountName=...;AccountKey=...;EndpointSuffix=...`
string (not just the account key).

**`403` / authorization failed**

- Confirm the container exists and matches `azure.container` exactly (case-sensitive).
- Confirm the `AccountKey` in the connection string is current — rotated keys invalidate old connection strings.

**No blobs appearing after several minutes**

Check collector logs for export errors. If logs show successful exports but
blobs don't appear, verify `azure.container` matches the real container name and
the connection string's `AccountName` matches `azure.url`.

## Upgrade

```bash
helm repo update speedscale-byoc
helm upgrade byoc-azureblob speedscale-byoc/azureblob -n byoc-azureblob --reuse-values \
  --set azure.url=https://mystorageacct.blob.core.windows.net/ \
  --set azure.container=byoc
```

Blobs already in Azure Blob Storage are unaffected by chart upgrades. Check the [CHANGELOG](CHANGELOG.md) for breaking changes and keep the collector image pinned by the chart.

## Configuration reference

| Key | Default | Description |
|---|---|---|
| `azure.url` | `https://CHANGEME.blob.core.windows.net/` | Storage account blob URL. `connection_string` overrides this, but it must be set. |
| `azure.container` | `byoc` | Blob container to write RRPairs into (must exist before installing) |
| `azure.connectionStringSecret` | `byoc-azureblob` | Name of the K8s Secret with the `connectionString` key |
| `image.otelCollector` | `otel/opentelemetry-collector-contrib:0.160.0@sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6` | Pinned OTel Collector Contrib image containing the `azure_blob` exporter |
