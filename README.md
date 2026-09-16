# Speedscale BYOC

Reference Helm charts for sending captured Speedscale traffic to storage and observability systems controlled by the customer.

## Destination channels

Every destination has one Forwarder exporter, one collector, one credential boundary, and one backend. The four primary channels are independent:

| Channel | Chart | Collector destination |
|---|---|---|
| S3 | [`charts/fluentbit-s3/`](charts/fluentbit-s3/) | Native OTel `awss3` exporter to one S3 bucket |
| GCS | [`charts/gcs/`](charts/gcs/) | Native OTel `google_cloud_storage` exporter to one GCS bucket |
| Datadog | [`charts/datadog/`](charts/datadog/) | Datadog exporter for logs, traces, and metrics |
| Dynatrace | [`charts/dynatrace/`](charts/dynatrace/) | OTLP/HTTP exporter for logs, traces, and metrics |

Additional charts remain independent destinations:

| Chart | Destination |
|---|---|
| [`charts/grafana/`](charts/grafana/) | Loki and Prometheus in the same Grafana stack |
| [`charts/elasticsearch/`](charts/elasticsearch/) | Elasticsearch and Kibana |
| [`charts/azureblob/`](charts/azureblob/) | Azure Blob Storage |
| [`charts/fluentbit-gcs/`](charts/fluentbit-gcs/) | Legacy GCS path using the S3-compatible API and HMAC |
| [`charts/otlp/`](charts/otlp/) | One generic OTLP logs backend without a dedicated chart |

## Wiring

Install the Speedscale Operator separately. Add one named Forwarder exporter for every enabled destination:

```yaml
forwarder:
  exporters:
    byoc_s3:
      otel_endpoint: http://otel-collector.byoc-s3.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
    byoc_gcs:
      otel_endpoint: http://byoc-gcs-gcs.byoc-gcs.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
    byoc_datadog:
      otel_endpoint: http://byoc-datadog-datadog.byoc-datadog.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
    byoc_dynatrace:
      otel_endpoint: http://byoc-dynatrace-dynatrace.byoc-dynatrace.svc.cluster.local:4317
      filter_rule: standard
      dlp_config_id: standard
```

Do not fan out from one destination collector to another backend. Splitting at the Forwarder keeps DLP rules, traffic filters, credentials, retry queues, failures, and enablement independent. A Datadog API key never belongs in GCS values, and a GCS identity never belongs in the Datadog chart.

## Install a channel

Each chart README contains its prerequisites and install command. For example:

```sh
helm repo add speedscale-byoc https://speedscale.github.io/speedscale-byoc/
helm repo update

helm upgrade --install byoc-datadog speedscale-byoc/datadog \
  --namespace byoc-datadog --create-namespace \
  --set datadog.credentialsSecret=datadog-partner-api-key
```

Create backend credentials outside Helm values and source control. Partner demos must use the dedicated partner-account credential Secret; production monitoring credentials must not be reused.

## Replay captured traffic

Object-store channels are imported from their own backend:

```sh
# S3
proxymock import s3 --bucket my-s3-archive --prefix byoc/ \
  --service api-gateway --from now-1h --out /tmp/s3-snapshot

# Native GCS
proxymock import gcs --bucket my-gcs-archive --prefix byoc/ \
  --service api-gateway --from now-1h --out /tmp/gcs-snapshot
```

Datadog retrieval queries full RRPair logs directly from Datadog:

```sh
python3 recipes/datadog-to-replay/gather.py \
  --trace-id <trace-id> --service api-gateway --out /tmp/datadog-capture
```

Companion scripts for legacy GCS, S3, Loki, Elasticsearch, Azure Blob, and generic Datadog log searches live in [`scripts/`](scripts/).

## Development checks

```sh
for chart in charts/*/; do helm lint --strict "$chart"; done
python3 tests/test_channel_boundaries.py
python3 -m unittest discover -s recipes/datadog-to-replay -p 'test_*.py' -v
```

The boundary test renders the four primary charts and verifies each collector contains only its declared destination exporter.

## License

Apache 2.0. See [LICENSE](LICENSE).
