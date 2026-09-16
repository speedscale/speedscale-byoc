# Native GCS traffic archive

This chart is the GCS channel. It receives Speedscale RRPairs over OTLP and writes every record to one private Google Cloud Storage bucket with the native OpenTelemetry `google_cloud_storage` exporter. It has no Datadog, Dynatrace, or S3 exporter.

## Configure

Create a private bucket and a Google identity for the collector. Grant the writer `roles/storage.objectCreator` on the bucket plus `storage.buckets.get`; grant readers `roles/storage.objectViewer` separately. The chart does not create buckets, IAM grants, or credentials.

```yaml
gcs:
  project: my-project
  bucket: my-private-traffic
  region: us-central1
  prefix: byoc
serviceAccount:
  create: true
  name: byoc-gcs
  annotations:
    iam.gke.io/gcp-service-account: byoc-gcs@my-project.iam.gserviceaccount.com
```

```sh
helm upgrade --install byoc-gcs speedscale-byoc/gcs \
  --namespace byoc-gcs --create-namespace -f values-gcs.yaml
```

Configure a dedicated Forwarder exporter for this collector:

```yaml
forwarder:
  exporters:
    byoc_gcs:
      otel_endpoint: http://byoc-gcs-gcs.byoc-gcs.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

Do not point `byoc_datadog`, `byoc_dynatrace`, or `byoc_s3` at this service. Each destination has its own collector so filters, DLP policy, credentials, failures, and lifecycle remain independent.

## Storage and replay

Objects are written below `byoc/year=.../month=.../day=.../hour=.../minute=.../logs_<uuid>.gz`. Apply capture filters and DLP before forwarding sensitive traffic.

```sh
proxymock import gcs --bucket my-private-traffic --prefix byoc/ --from now-1h
```

Local imports use Application Default Credentials. Authenticate only the account allowed to read the bucket. A collector writer identity does not grant local read access.

## Dedicated cluster reader

The optional reader uses a separately qualified proxymock image and a separate Google identity with `roles/storage.objectViewer`.

```yaml
reader:
  enabled: true
  image: YOUR_QUALIFIED_READER_IMAGE_DIGEST
  serviceAccount:
    create: true
    name: byoc-gcs-reader
  rbac:
    create: true
    subjects:
      - kind: Group
        name: traffic-readers
        apiGroup: rbac.authorization.k8s.io
```

The reader listens on pod loopback and is reached through authenticated Kubernetes port forwarding. Its Role grants ConfigMap discovery and access to the named reader pod's port-forward subresource; it grants no Secret or pod-exec access.

```sh
proxymock import gcs --list-buckets --bucket-namespace byoc-gcs
proxymock import gcs --bucket-from-cluster --bucket-namespace byoc-gcs \
  --destination byoc-gcs/byoc-gcs-gcs/google_cloud_storage \
  --access-mode cluster --from now-1h
```

## Validate

Send traced and untraced records, including records with identical timestamps, and verify every UUID and payload survives export and import. Monitor exporter failures and queue saturation. Queues are memory-only, retries expire after five minutes, collector restarts can lose queued records, and retries can create duplicates.

The collector image is pinned in `values.yaml`. Review the [GCS exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/googlecloudstorageexporter) before upgrading.

## Migration from `fluentbit-gcs`

`fluentbit-gcs` uses the S3-compatible XML API and HMAC credentials. This chart uses Google's native exporter and Workload Identity. Install it under a separate release, validate the new prefix, and then change only `forwarder.exporters.byoc_gcs.otel_endpoint`. Existing objects remain readable.
