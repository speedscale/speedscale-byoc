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

### Credentials outside GKE

The default `auth.mode: ambient` uses Application Default Credentials from the environment, including GKE Workload Identity. For a self-hosted cluster, choose one of these modes for the collector. Create the referenced Secret or ConfigMap in the chart namespace before installing the chart.

For a service-account key, store the JSON file in a Kubernetes Secret outside Helm values and source control:

```sh
kubectl -n byoc-gcs create secret generic gcs-writer \
  --from-file=credentials.json=/secure/path/writer-key.json
```

```yaml
auth:
  mode: secret
  secretName: gcs-writer
```

For keyless authentication, [configure Google Workload Identity Federation for self-hosted Kubernetes](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-kubernetes). Generate its credential configuration with `/var/run/service-account/token` as the credential source file, then create a ConfigMap from that file:

```sh
kubectl -n byoc-gcs create configmap gcs-writer-identity \
  --from-file=credentials.json=/secure/path/credential-configuration.json
```

```yaml
auth:
  mode: federation
  configMapName: gcs-writer-identity
  audience: https://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID
```

Both modes mount the file at `/var/run/google/credentials.json` and set `GOOGLE_APPLICATION_CREDENTIALS` on the collector. Federation also projects a renewable Kubernetes service-account token at `/var/run/service-account/token`. Set `reader.auth` to the same mode with a separate Secret or ConfigMap and a read-only Google identity when enabling the cluster reader. The reader never uses the collector's credential settings.

The delivery queue is memory-only by default and does not create a PersistentVolumeClaim. Enable persistence only when the cluster permits dynamic volume provisioning:

```yaml
delivery:
  queueSize: 1000
  persistence:
    enabled: true
    size: 2Gi
```

A persistent queue retains buffered records when the collector pod is replaced. The default memory queue loses buffered records when the collector restarts, but avoids a storage dependency.

When upgrading from chart 1.1.0, set `delivery.persistence.enabled=true` to retain the existing PVC-backed queue. Without that setting, Helm removes the queue PVC during the upgrade.

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

Send traced and untraced records, including records with identical timestamps, and verify every UUID and payload survives export and import. Monitor exporter failures and queue saturation. Retries do not expire, collector restarts can lose records from the default memory queue, and retries can create duplicates.

The collector image is pinned in `values.yaml`. Review the [GCS exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/googlecloudstorageexporter) before upgrading.

## Migration from `fluentbit-gcs`

`fluentbit-gcs` uses the S3-compatible XML API and HMAC credentials. This chart uses Google's native exporter and the Google credential mode selected above. Install it under a separate release, validate the new prefix, and then change only `forwarder.exporters.byoc_gcs.otel_endpoint`. Existing objects remain readable.
