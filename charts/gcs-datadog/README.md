# GCS traffic archive with Datadog trace links

This chart receives Speedscale RRPair logs over OTLP, writes the full records to a private GCS bucket, and sends a short GCS console URL to Datadog with the original trace and span IDs. It uses native GCS authentication through Workload Identity. No S3 interoperability keys are required.

This reference requires a forwarder release that emits native OTLP log trace/span IDs and `resource.service.name`. Release validation is pending. Keep deployments pinned to versions validated together.

## Configure

Create a private bucket and enable Workload Identity Federation for GKE. Grant the collector Kubernetes service account `roles/storage.objectCreator` on that bucket plus a custom role containing `storage.buckets.get`. The exporter checks the bucket at startup, even when it already exists. Grant readers `roles/storage.objectViewer` separately. The chart does not create buckets, IAM grants, or credentials.

Create a Kubernetes Secret containing the Datadog API key in the release namespace through your secret manager. Reference its name and key in values; keep the key out of Helm values and source control.

```yaml
# values-gcs.yaml
gcs:
  project: my-project
  bucket: my-private-traffic
  region: us-central1
  prefix: byoc
serviceAccount:
  create: true
  name: byoc-collector
datadog:
  enabled: true
  site: datadoghq.com
  credentialsSecret: datadog-api-key
  apiKeyField: api-key
```

For direct Workload Identity Federation, bind the principal for `namespace/byoc-collector` to the bucket roles. When using an annotated Google service account instead, configure `serviceAccount.annotations.iam.gke.io/gcp-service-account` and the corresponding impersonation binding.

```sh
helm upgrade --install byoc ./charts/gcs-datadog \
  --namespace speedscale --create-namespace -f values-gcs.yaml
```

Keep this exporter configuration in the operator release's managed values:

```yaml
forwarder:
  exporters:
    byoc_otel:
      otel_endpoint: http://byoc-gcs-datadog.speedscale.svc:4317
      filter_rule: standard
      dlp_config_id: standard
```

Point the Speedscale forwarder's `byoc_otel` exporter at `http://byoc-gcs-datadog.speedscale.svc:4317`. Send application OTLP traces to the same collector if Datadog does not already receive them. The chart forwards traces to Datadog when enabled. Matching IDs alone do not create APM spans.

## Routing and retrieval

Full records are stored below `byoc/<service.name>/<trace_id>/year=.../month=.../day=.../hour=.../minute=.../logs_<uuid>.gz`. The Datadog log body is the GCS console URL for that service and trace prefix. It retains native trace/span IDs and the legacy `service` log attribute. Opening a URL requires the reader's own bucket permission.

Both log pipelines discard records without a nonzero native trace ID or a service name. This reference accepts service names matching `[A-Za-z0-9][A-Za-z0-9._-]*`; other names are discarded to keep object paths and console URLs consistent. Check your actual service names before enabling the filter. Application instrumentation must propagate W3C traceparent or B3 headers for captured HTTP requests to have trace context.

The GCS exporter chooses a destination using the first resource in a request. The chart groups by service and trace, then limits each GCS export batch to one record to prevent mixed destinations. This creates one object per record and increases operation cost; measure throughput and object volume with representative traffic. Do not increase this batch size without proving routing isolation.

Use native GCS import to retrieve one trace:

```sh
proxymock import gcs --bucket my-private-traffic \
  --prefix byoc/checkout/4bf92f3577b34da6a3ce929d0e0e4736/
```

## Validation and operation

Send interleaved traced requests to two captured services, including W3C and B3 headers. Read the resulting GCS objects and verify every embedded resource matches its object prefix, the HTTP body remains intact, and untraced requests are absent. Verify that the corresponding Datadog log opens the expected GCS prefix and links to the actual APM span. Set `datadog.enabled=false` for an isolated GCS test; correlation logs then go to collector stdout and application traces are not accepted.

The archive and Datadog pipelines are independent. A URL can arrive before its object, or remain visible after an archive failure. Queues are memory-only and retries expire after five minutes for GCS; collector restarts can lose queued records and retries can create duplicates. Monitor exporter failures and queue saturation, and establish retention and access policies for captured payloads. This reference is not a lossless archive guarantee.

Keep `values-gcs.yaml`, the forwarder exporter endpoint, capture selectors, and image versions in the source managed by your deployment system. If Terraform manages Helm, pass the values file through the `helm_release.values` input and expose the same setting from any wrapper module. Manual Helm or kubectl overrides will be replaced by the next managed apply.

The collector is pinned to the version and digest in values. Review the [GCS exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/googlecloudstorageexporter) and [Datadog exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/datadogexporter) before upgrading.
