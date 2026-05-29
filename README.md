# Speedscale BYOC

Reference architecture Helm charts for Speedscale BYOC (Bring Your Own Cloud) — capture real traffic with the Speedscale Operator and route it to your own storage backend instead of (or in addition to) Speedscale Cloud.

## Scenarios

| Chart | Stack | Best for |
|---|---|---|
| [`charts/grafana/`](charts/grafana/) | OTel Collector → Loki + Prometheus → Grafana | Live dashboard + PromQL aggregates + LogQL drill-down |
| [`charts/elasticsearch/`](charts/elasticsearch/) | OTel Collector → Elasticsearch → Kibana | Full-text search + Kibana Discover |
| [`charts/fluentbit-gcs/`](charts/fluentbit-gcs/) | OTel Collector → Fluent Bit → GCS | Durable GCS archive + BigQuery |
| [`charts/fluentbit-s3/`](charts/fluentbit-s3/) | OTel Collector → Fluent Bit → S3 | Durable S3 archive + Athena |

All scenarios coexist in separate namespaces on the same cluster. Flip the Forwarder's `byoc_otel.otel_endpoint` to switch which backend receives traffic.

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
│   ├── grafana/          # OTel Collector + Loki + Prometheus + Grafana
│   ├── elasticsearch/    # Elasticsearch + Kibana + OTel Collector
│   ├── fluentbit-gcs/    # OTel Collector + Fluent Bit → Google Cloud Storage
│   └── fluentbit-s3/     # OTel Collector + Fluent Bit → Amazon S3
└── scripts/
    ├── loki-gather.py    # Pull RRPairs from Loki → proxymock snapshot
    ├── es-gather.py      # Pull RRPairs from Elasticsearch → proxymock snapshot
    ├── gcs-gather.py     # Pull RRPairs from GCS → proxymock snapshot
    └── s3-gather.py      # Pull RRPairs from S3 → proxymock snapshot
```

## Replay captured traffic with proxymock

Each scenario ships a companion `scripts/<backend>-gather.py` that queries a time window of captured traffic and writes a [`proxymock`](https://docs.speedscale.com/proxymock/)-replayable directory:

```bash
# Grafana scenario
python3 scripts/loki-gather.py \
  --loki-url http://<node-ip>:30031 --service my-service --start -1h \
  --out-dir /tmp/snapshot

# Elasticsearch scenario
python3 scripts/es-gather.py \
  --es-url http://<node-ip>:30032 --service my-service --start -1h \
  --out-dir /tmp/snapshot

# GCS scenario
python3 scripts/gcs-gather.py \
  --bucket my-rrpair-archive --service my-service --start -1h \
  --out-dir /tmp/snapshot

# S3 scenario
python3 scripts/s3-gather.py \
  --bucket my-rrpair-archive --region us-east-1 --service my-service --start -1h \
  --out-dir /tmp/snapshot

proxymock mock --in /tmp/snapshot
```

See [`scripts/README.md`](scripts/README.md) for all filter flags.

## License

Apache 2.0 — see [LICENSE](LICENSE).
