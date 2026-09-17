# Datadog channel

This chart is the Datadog channel. Its collector exports OTLP logs, traces, and metrics only to Datadog. It contains no GCS, S3, or Dynatrace configuration.

Create a Kubernetes Secret containing the API key for the intended Datadog organization. Partner demos must use the dedicated partner account Secret and must never reuse the production monitoring key.

```sh
kubectl -n byoc-datadog create secret generic datadog-partner-api-key \
  --from-literal=api-key='<PARTNER_API_KEY>'

helm upgrade --install byoc-datadog speedscale-byoc/datadog \
  --namespace byoc-datadog --create-namespace \
  --set datadog.site=datadoghq.com \
  --set datadog.credentialsSecret=datadog-partner-api-key
```

Configure an independent Forwarder exporter:

```yaml
forwarder:
  exporters:
    byoc_datadog:
      otel_endpoint: http://byoc-datadog-datadog.byoc-datadog.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

The Forwarder sends captured RRPairs through the logs pipeline. Applications can send their OTLP traces and metrics to the same Datadog collector service. The collector extracts W3C trace context from captured request headers, copies the RRPair workload into `service.name`, maps HTTP 5xx responses and exception events to span errors, and generates the trace metrics used by Datadog APM service views. The three signals share one Datadog destination and API key; they do not share a collector with any other backend.

Verify collector export errors first, then query the selected Datadog organization for recent logs with `@msgType:rrpair`, APM traces by `service`, and metrics by their instrument names. Confirm the organization before enabling traffic.

The API key only permits ingest. Query tools require a same-organization application key. The partner demo recipe requires `DATADOG_PARTNER_API_KEY`, `DATADOG_PARTNER_APP_KEY`, and `DATADOG_PARTNER_SITE` explicitly and never falls back to production credentials.
