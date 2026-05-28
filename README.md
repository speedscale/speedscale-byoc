# Speedscale BYOC

Reference architecture Helm charts for Speedscale BYOC (Bring Your Own Cloud) — capture real traffic with the Speedscale Operator and route it to your own storage backend instead of (or in addition to) Speedscale Cloud.

## Scenarios

| Chart | Stack | Use when |
|---|---|---|
| [`charts/grafana/`](charts/grafana/) | OTel Collector → Loki → Grafana | You want a live query + dashboard UI |
| [`charts/elasticsearch/`](charts/elasticsearch/) | OTel Collector → Elasticsearch → Kibana | You want full-text search + Kibana Discover |
| [`charts/fluentbit/`](charts/fluentbit/) | OTel Collector → Fluent Bit → GCS | You want durable object-storage archive + BigQuery |

All three coexist in separate namespaces on the same cluster. Flip the Forwarder's `byoc_otel.otel_endpoint` to switch which backend receives traffic.

## Quick start

```bash
helm repo add speedscale https://speedscale.github.io/operator-helm/
helm repo add speedscale-byoc https://speedscale.github.io/speedscale-byoc/
helm repo update

# Install the Speedscale Operator + Forwarder
helm upgrade --install speedscale-operator speedscale/speedscale-operator \
  -n speedscale --create-namespace \
  --set apiKeySecret=speedscale-apikey \
  --set clusterName=my-cluster \
  --set 'forwarder.exporters.byoc_otel.otel_endpoint=http://otel-collector.byoc-grafana.svc.cluster.local:4317'

# Pick a backend — e.g. Grafana + Loki
helm upgrade --install byoc-grafana speedscale-byoc/grafana \
  -n byoc-grafana --create-namespace
```

See each chart's `README.md` for the full install + configure + replay walkthrough.

## Repository layout

```
speedscale-byoc/
├── charts/
│   ├── grafana/          # Loki + Grafana + OTel Collector + Prometheus
│   ├── elasticsearch/    # Elasticsearch + Kibana + OTel Collector
│   └── fluentbit/        # OTel Collector + Fluent Bit → GCS
└── scripts/
    ├── loki-gather.py    # Pull RRPairs from Loki → proxymock snapshot
    ├── es-gather.py      # Pull RRPairs from ES → proxymock snapshot
    └── gcs-gather.py     # Pull RRPairs from GCS → proxymock snapshot
```

## Replay captured traffic with proxymock

Each scenario ships a companion `scripts/<backend>-gather.py` script that queries any subset of your captured traffic and writes a [`proxymock`](https://docs.speedscale.com/proxymock/)-replayable directory. See [`scripts/README.md`](scripts/README.md) for usage.

```bash
python3 scripts/loki-gather.py \
  --loki-url http://<node-ip>:30031 \
  --service my-service --status 2.. --start -1h \
  --out-dir /tmp/snapshot

proxymock mock --in /tmp/snapshot
```

## Operator values

Each chart ships example Speedscale Operator values under `examples/operator-values.yaml` that wire the Forwarder's `byoc_otel` exporter to the chart's OTel Collector endpoint.

## License

Apache 2.0 — see [LICENSE](LICENSE).
