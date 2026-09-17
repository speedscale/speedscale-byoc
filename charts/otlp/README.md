# Generic OTLP logs channel

This chart forwards Speedscale logs, traces, and metrics to one OTLP/HTTP backend that does not have a dedicated chart. Use `charts/datadog`, `charts/dynatrace`, `charts/newrelic`, `charts/splunk`, `charts/gcs`, or `charts/fluentbit-s3` for those destinations. Do not use this chart to combine them.

Configure the exact logs endpoint, auth header, and a Secret containing the token under key `token`:

```yaml
otlp:
  endpoint: https://api.honeycomb.io/v1/logs
  headerName: x-honeycomb-team
  headerPrefix: ""
  tokenSecret: byoc-honeycomb
```

```sh
helm upgrade --install byoc-honeycomb speedscale-byoc/otlp \
  --namespace byoc-honeycomb --create-namespace \
  -f charts/otlp/examples/honeycomb.yaml
```

Give this installation its own Forwarder exporter, DLP configuration, and filter rule. The collector exports logs only to the configured endpoint.

```yaml
forwarder:
  exporters:
    byoc_honeycomb:
      otel_endpoint: http://byoc-honeycomb-otel-collector.byoc-honeycomb.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

The Secret is created outside the chart. Check collector logs for authentication, endpoint, payload-size, and rate-limit errors. This generic chart accepts one destination per release.

For security tools, enable `securityProfile.enabled`. It maps RRPair fields to common event, HTTP, network, workload, DLP, and trace-correlation attributes. Its default `includeFullPayload: false` replaces the captured body with normalized metadata. Examples for Elastic Security, Falcon LogScale, and OpenSearch Security Analytics are in [`examples/`](examples/).
