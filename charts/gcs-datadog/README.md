# Native GCS traffic archive

This chart receives RRPair logs over OTLP and archives them in a private GCS bucket using Workload Identity. The default `archive` profile retains traced and untraced records. Datadog is disabled by default. Select `trace-correlation` to archive traced traffic by service and trace ID and optionally send Datadog logs containing GCS console links.

The trace-correlation profile requires a forwarder release that emits native OTLP log trace/span IDs and `resource.service.name`. Public-release qualification is pending. Keep deployments pinned to versions validated together.

## Configure

Create a private bucket and enable Workload Identity Federation for GKE. Grant the collector Kubernetes service account `roles/storage.objectCreator` on that bucket plus a custom role containing `storage.buckets.get`. The exporter checks the bucket at startup, even when it already exists. Grant readers `roles/storage.objectViewer` separately. The chart does not create buckets, IAM grants, or credentials.

For the optional Datadog profile, create a Kubernetes Secret containing the Datadog API key in the release namespace through your secret manager. Reference its name and key in values; keep the key out of Helm values and source control.

```yaml
# values-gcs.yaml
profile: archive
gcs:
  project: my-project
  bucket: my-private-traffic
  region: us-central1
  prefix: byoc
serviceAccount:
  create: true
  name: byoc-collector
datadog:
  enabled: false
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

The default archive profile stores records below `byoc/year=.../month=.../day=.../hour=.../minute=.../logs_<uuid>.gz`. It does not filter on trace IDs or service-name spelling. Apply capture filters and DLP upstream before forwarding sensitive traffic.

Enable the separate correlation profile with this values overlay:

```yaml
profile: trace-correlation
datadog:
  enabled: true
  site: datadoghq.com
  credentialsSecret: datadog-api-key
  apiKeyField: api-key
```


In the correlation profile, full records are stored below `byoc/<service.name>/<trace_id>/year=.../month=.../day=.../hour=.../minute=.../logs_<uuid>.gz`. The Datadog log body is the GCS console URL for that service and trace prefix. It retains native trace/span IDs and the legacy `service` log attribute. Opening a URL requires the reader's own bucket permission.

The correlation profile discards records without a nonzero native trace ID or a service name. This reference accepts service names matching `[A-Za-z0-9][A-Za-z0-9._-]*`; other names are discarded to keep object paths and console URLs consistent. Check your actual service names before enabling the filter. Application instrumentation must propagate W3C traceparent or B3 headers for captured HTTP requests to have trace context.

For trace routing, the GCS exporter chooses a destination using the first resource in a request. The correlation profile groups by service and trace, then limits each GCS export batch to one record to prevent mixed destinations. This creates one object per record and increases operation cost; measure throughput and object volume with representative traffic. Do not increase this batch size without proving routing isolation.

Local imports use the credentials of the machine running proxymock. Run `gcloud auth application-default login`, or configure `GOOGLE_APPLICATION_CREDENTIALS` for an approved workload identity. Grant that reader `roles/storage.objectViewer` separately; the collector identity is not copied to the local shell.

Import the archive:

```sh
proxymock import gcs --bucket my-private-traffic --prefix byoc/ --from now-1h
```

For the correlation profile, retrieve one trace:

```sh
proxymock import gcs --bucket my-private-traffic \
  --prefix byoc/checkout/4bf92f3577b34da6a3ce929d0e0e4736/
```

## Dedicated cluster reader

The optional reader requires a proxymock image containing `bucket-reader`. That image and the CLI must be qualified together before enabling this feature; public-release qualification is pending. Set `reader.image` explicitly to the qualified image digest.

```yaml
reader:
  enabled: true
  image: YOUR_QUALIFIED_READER_IMAGE_DIGEST
  serviceAccount:
    create: true
    name: byoc-reader
  rbac:
    create: true
    subjects:
      - kind: Group
        name: YOUR_KUBERNETES_READER_GROUP
        apiGroup: rbac.authorization.k8s.io
```

Give `byoc-reader` a separate Google identity with `roles/storage.objectViewer` on the archive bucket. For direct Workload Identity Federation, grant the bucket role to this Kubernetes service account principal. For service-account impersonation, configure its `reader.serviceAccount.annotations` and the Google impersonation binding. Keep collector write permission separate. Snapshot uploads require a separately authorized writer; this reader supports only list and get operations.

The chart publishes the bucket, prefix, and reader pod name in the collector ConfigMap. The reader listens on pod loopback and is reached through authenticated Kubernetes port-forwarding. Optional RBAC permits ConfigMap discovery in the release namespace and access to the named reader pod's port-forward subresource. It grants no Secret or pod-exec access. No Google credential is returned to the laptop.

Use the CLI built with reader support to discover destinations, then select the exact returned ID:

```sh
proxymock import gcs --list-buckets --bucket-namespace speedscale
proxymock import gcs --bucket-from-cluster --bucket-namespace speedscale --destination speedscale/byoc-gcs-datadog/google_cloud_storage --access-mode cluster --from now-1h
```

Use `--kube-context` to select a cluster. Multiple collector destinations require an explicit `--destination`. The reader limits requests to the configured archive prefix and uses its own renewable Google credentials. If the reader is absent or access is denied, check its rollout, Kubernetes port-forward permission, and Google object-viewer grant. The CLI does not fall back to a different identity. Select `--access-mode adc` explicitly to use local Google credentials with a discovered destination.

## Validation and operation

For the archive profile, send traced and untraced records, including distinct records with identical timestamps, and verify every UUID and payload survives export and import. For the correlation profile, send interleaved traced requests to two captured services, including W3C and B3 headers. Read the resulting GCS objects and verify every embedded resource matches its object prefix, the HTTP body remains intact, and untraced requests are absent. Verify that the corresponding Datadog log opens the expected GCS prefix and links to the actual APM span. With `profile=trace-correlation` and `datadog.enabled=false`, correlation links go to collector stdout and application traces are not accepted. The default archive profile exports only to GCS.

The archive and Datadog pipelines are independent. A URL can arrive before its object, or remain visible after an archive failure. Queues are memory-only and retries expire after five minutes for GCS; collector restarts can lose queued records and retries can create duplicates. Monitor exporter failures and queue saturation, and establish retention and access policies for captured payloads. This reference is not a lossless archive guarantee.

Keep `values-gcs.yaml`, the forwarder exporter endpoint, capture selectors, and image versions in the source managed by your deployment system. If Terraform manages Helm, pass the values file through the `helm_release.values` input and expose the same setting from any wrapper module. Manual Helm or kubectl overrides will be replaced by the next managed apply.

The collector is pinned to the version and digest in values. Review the [GCS exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/googlecloudstorageexporter) and [Datadog exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.160.0/exporter/datadogexporter) before upgrading.

## Migration from the historical GCS chart

`fluentbit-gcs` uses S3 interoperability and HMAC credentials. This chart uses the native `google_cloud_storage` exporter. Install it under a separate release name, configure the Google writer identity, then point the forwarder's managed OTLP exporter values at the new collector service. Existing objects remain readable through native `proxymock import gcs`; retain the old prefix when importing historical traffic. Keep the old collector available until the new path has passed payload and identity checks.
