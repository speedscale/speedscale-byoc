# New Relic channel

This chart sends Speedscale RRPair logs and application OpenTelemetry data to one New Relic account. It has independent credentials, retries, and enablement from every other BYOC destination.

Create an ingest license key in the intended New Relic account and store it in a Kubernetes Secret outside source control:

```sh
kubectl create namespace byoc-newrelic
kubectl -n byoc-newrelic create secret generic newrelic-license-key \
  --from-literal=license-key='<NEW_RELIC_LICENSE_KEY>'

helm upgrade --install byoc-newrelic speedscale-byoc/newrelic \
  --namespace byoc-newrelic \
  --set newrelic.credentialsSecret=newrelic-license-key
```

The default endpoint is the New Relic US OTLP endpoint. Set `newrelic.endpoint` to the documented EU, Japan, or FedRAMP OTLP base URL when required. Do not add `/v1/logs`, `/v1/traces`, or `/v1/metrics`; the Collector appends the signal path.

Configure an independent Forwarder exporter for captured RRPairs:

```yaml
forwarder:
  exporters:
    byoc_newrelic:
      otel_endpoint: http://byoc-newrelic-newrelic.byoc-newrelic.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

Applications send OTLP traces and metrics to the same collector service. The collector gives each RRPair a readable `<direction> <method> <path>` message, groups records by `service.name` before export, extracts W3C trace context from captured request headers, and marks spans with HTTP 5xx responses or exception events as errors. A span-metrics pipeline derives `http.server.duration` from server spans so New Relic can populate APM service views even when the application sends traces without compatible APM metrics.

Verify export errors in the collector logs, then use New Relic **APM & Services** for services and distributed traces, **Logs** for `msgType = 'rrpair'`, and **Metrics and events** for application metrics. An account ID is not part of the ingest configuration and should not be stored in this chart.
