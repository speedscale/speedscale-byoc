# Apache Iceberg sink

This chart is the Iceberg channel. It runs the Speedscale `iceberg-sink`, which receives RRPairs from the Forwarder over OTLP/gRPC and appends them to one Apache Iceberg table. Any engine that reads Iceberg (Athena, Spark, Trino, Snowflake, DuckDB) can then query captured traffic. It supports AWS S3 Tables, AWS Glue, and any Iceberg REST catalog (Polaris, Lakekeeper, Nessie, Unity, Snowflake Open Catalog), with data in S3, GCS, or Azure storage.

The sink is a separate pod rather than a collector exporter, so Iceberg libraries and catalog credentials stay out of the Forwarder.

## Table

One row per RRPair, partitioned by `day(ts)` and `cluster`:

`ts`, `uuid`, `cluster`, `namespace`, `service`, `direction`, `l7protocol`, `tech`, `command`, `location`, `status`, `elapsed_us`, `is_tls`, `tags` (map), `dlp_modified`, `rrpair` (the full RRPair as protojson), `ingested_at`.

The sink creates the namespace and table when they are missing. A pre-existing table must have every column above.

Delivery is at-least-once. A retried export can write the same RRPair twice, so dedupe on `uuid` when exact counts matter:

```sql
SELECT * FROM speedscale.rrpairs
QUALIFY row_number() OVER (PARTITION BY uuid ORDER BY ingested_at) = 1
```

## Configure

Choose a catalog with `iceberg.catalog.type`. Create the catalog, table bucket or warehouse, and identity first; the chart does not create cloud resources or IAM grants.

### AWS S3 Tables

The warehouse is the table bucket ARN. S3 Tables compacts and expires snapshots itself and denies client deletes, so the sink's own maintenance stays off (`maintenance: auto`).

```yaml
iceberg:
  catalog:
    type: s3tables
    uri: ""
    warehouse: arn:aws:s3tables:us-east-1:111122223333:bucket/speedscale-traffic
awsRegion: us-east-1
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111122223333:role/byoc-iceberg
```

The role needs `s3tables:GetTableBucket`, `s3tables:GetNamespace`, `s3tables:CreateNamespace`, `s3tables:GetTable`, `s3tables:CreateTable`, `s3tables:GetTableMetadataLocation`, `s3tables:UpdateTableMetadataLocation`, `s3tables:GetTableData` and `s3tables:PutTableData` on the table bucket and its tables ([S3 Tables Iceberg REST endpoint permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html#endpoint-supported-api)).

### AWS Glue

```yaml
iceberg:
  catalog:
    type: glue
    uri: ""
    warehouse: s3://my-lake/warehouse
awsRegion: us-east-1
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111122223333:role/byoc-iceberg
```

The role needs Glue database and table read, create and update permissions plus S3 read and write on the warehouse prefix. After the sink creates the table, [enable compaction](https://docs.aws.amazon.com/glue/latest/dg/enable-compaction.html) and [snapshot retention](https://docs.aws.amazon.com/glue/latest/dg/enable-snapshot-retention.html) optimizers on it with an optimizer IAM role. Glue turns both off for new tables, and maintenance in the sink stays off by default for Glue, so without them small files and snapshots accumulate.

### REST catalog

```yaml
iceberg:
  catalog:
    type: rest
    uri: https://polaris.example.com/api/catalog
    warehouse: speedscale
    propertiesSecret:
      name: iceberg-catalog
      key: catalog-properties.json
```

`propertiesSecret` holds `ICEBERG_CATALOG_PROPERTIES`: a JSON object of iceberg-go catalog and FileIO properties. Keep catalog credentials there, not in values:

```sh
kubectl -n byoc-iceberg create secret generic iceberg-catalog \
  --from-literal=catalog-properties.json='{"credential":"CLIENT_ID:CLIENT_SECRET","scope":"PRINCIPAL_ROLE:ALL"}'
```

Non-secret properties can go in `iceberg.catalog.properties` instead. Every value must be a string, for example `"s3.endpoint": "http://minio.minio.svc:9000"`. The sink expires old snapshots and metadata itself for REST catalogs (`maintenance: auto`). Data files still need compaction; see [Compaction](#compaction).

### Storage credentials

The default `auth.mode: ambient` uses the cloud SDK default credential chain. Annotate the service account for workload identity:

| Platform | Setting |
|---|---|
| EKS IRSA | `serviceAccount.annotations."eks.amazonaws.com/role-arn"` |
| EKS Pod Identity | Associate the role with the service account; no annotation |
| GKE Workload Identity | `serviceAccount.annotations."iam.gke.io/gcp-service-account"` |
| Azure Workload Identity | `serviceAccount.annotations."azure.workload.identity/client-id"` and `azureWorkloadIdentity: true` |

Without workload identity, set `auth.mode: secret`. Every key of `auth.secretName` is loaded as an environment variable, so store the variables your SDK reads, for example `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. For a Google service-account key, add it to the same Secret and set `auth.googleCredentialsKey` to its key name; the chart mounts it and sets `GOOGLE_APPLICATION_CREDENTIALS`.

```yaml
auth:
  mode: secret
  secretName: iceberg-storage
```

### Install

```sh
helm upgrade --install byoc-iceberg speedscale-byoc/iceberg \
  --namespace byoc-iceberg --create-namespace -f values-iceberg.yaml
```

Then add a dedicated Forwarder exporter in the Speedscale Operator values:

```yaml
forwarder:
  exporters:
    byoc_iceberg:
      otel_endpoint: http://byoc-iceberg-iceberg.byoc-iceberg.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

The NetworkPolicy admits OTLP from the release namespace and `networkPolicy.allowedNamespaces` (default `speedscale`). Add your Prometheus namespace to `networkPolicy.metricsNamespaces` to scrape `/metrics`.

## Sizing and commits

| Value | Default | Meaning |
|---|---|---|
| `iceberg.commit.interval` | `60s` | Longest time before buffered rows are committed |
| `iceberg.commit.maxBatchBytes` | `8388608` | Commit early at this buffered size |
| `iceberg.commit.maxPendingBytes` | `67108864` | Past this, the sink returns `UNAVAILABLE` and the Forwarder retries |
| `iceberg.commit.retries` | `10` | Written to the table as `commit.retry.num-retries` |
| `iceberg.maintenance` | `auto` | Snapshot expiry and metadata cleanup: on for `rest`, off for `glue` and `s3tables` |

The chart sets `GOMEMLIMIT` to 80% of `resources.limits.memory`; override it with `goMemLimit`. With an 8 MiB batch, a 2,000 RRPair/s stream needs about 60 MiB of heap, so the default 512Mi limit leaves room.

`replicaCount` defaults to 1. Several replicas can commit to one table safely. On shutdown each pod keeps serving for `preStopSleepSeconds` while the Service drops it, then commits its buffered rows before exiting. Keep `terminationGracePeriodSeconds` above that delay plus one commit.

## Compaction

Each commit writes about one data file per active partition, so a 60 s interval creates about 1,440 files per partition per day for each replica. S3 Tables compacts these for you, and Glue does once its compaction optimizer is enabled. A self-managed REST catalog needs a scheduled rewrite, which the optional CronJob runs with the same catalog settings and credentials as the sink:

```yaml
compaction:
  enabled: true
  schedule: "17 */6 * * *"
  image: YOUR_COMPACTION_IMAGE
  command: ["/opt/compact"]
  args: []
  resources:
    requests: {cpu: 500m, memory: 1Gi}
    limits: {memory: 1Gi}
```

The job receives `ICEBERG_CATALOG_TYPE`, `ICEBERG_CATALOG_URI`, `ICEBERG_WAREHOUSE`, `ICEBERG_NAMESPACE`, `ICEBERG_TABLE`, `ICEBERG_CATALOG_PROPERTIES`, the storage credentials, and a `GOMEMLIMIT` at 80% of its memory limit. Point `image` and `command` at the engine you use for Iceberg `rewrite_data_files`. The memory limit must be at least 1Gi: an iceberg-go bin-pack of 4,032 small files peaked at 653 MiB of heap in testing. Set `compaction.podSecurityContext` and `compaction.securityContext` when the engine image needs another user or a writable root filesystem.

## Metrics

The sink serves `/healthz`, `/readyz` and Prometheus `/metrics` on port 8080. Set `metrics.serviceMonitor.enabled=true` with the Prometheus Operator.

`iceberg_sink_rows_received_total`, `iceberg_sink_rows_committed_total`, `iceberg_sink_rows_dropped_total{reason}`, `iceberg_sink_commits_total{result}`, `iceberg_sink_commit_seconds`, `iceberg_sink_pending_bytes`, `iceberg_sink_maintenance_total{result}`, `iceberg_sink_records_skipped_total`, `iceberg_sink_records_invalid_total`.

Alert on `iceberg_sink_commits_total{result!="ok"}` and on `iceberg_sink_pending_bytes` approaching `maxPendingBytes`.

## Try it locally

[`examples/local-rest-catalog.yaml`](examples/local-rest-catalog.yaml) runs an S3-compatible store and the Apache Iceberg REST catalog fixture with throwaway credentials, and [`examples/values-local-rest.yaml`](examples/values-local-rest.yaml) points the sink at it. Use them only for a local demo.
