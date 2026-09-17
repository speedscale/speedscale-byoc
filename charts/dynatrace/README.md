# Dynatrace channel

This chart is the Dynatrace channel. Its collector exports OTLP logs, traces, and metrics only to one Dynatrace environment. It contains no GCS, S3, or Datadog configuration.

Create an access token with `logs.ingest`, `metrics.ingest`, and `openTelemetryTrace.ingest`, then store it in a Kubernetes Secret outside source control.

```sh
kubectl -n byoc-dynatrace create secret generic dynatrace-api-token \
  --from-literal=api-token='<DYNATRACE_TOKEN>'

helm upgrade --install byoc-dynatrace speedscale-byoc/dynatrace \
  --namespace byoc-dynatrace --create-namespace \
  --set dynatrace.endpoint=https://<environment>.live.dynatrace.com/api/v2/otlp
```

Configure an independent Forwarder exporter:

```yaml
forwarder:
  exporters:
    byoc_dynatrace:
      otel_endpoint: http://byoc-dynatrace-dynatrace.byoc-dynatrace.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

The Forwarder sends RRPairs through the logs pipeline. Applications can send OTLP traces and metrics to the same Dynatrace collector service. The collector extracts W3C trace context from captured request headers, copies the RRPair workload into `service.name`, maps HTTP 5xx responses and exception events to span errors, and converts cumulative metrics to delta temporality. It appends `/v1/logs`, `/v1/traces`, or `/v1/metrics` to the configured base endpoint.

Verify the collector has no export errors, then use Dynatrace Logs or a Notebook to query recent RRPairs. Trace and service views require application spans; Speedscale capture logs alone do not synthesize APM spans.

For Dynatrace SaaS use `https://<environment>.live.dynatrace.com/api/v2/otlp`. For Managed use the environment-specific `/api/v2/otlp` base. Do not include a signal suffix in `dynatrace.endpoint`.
