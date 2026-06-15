# Speedscale BYOC

Reference architecture Helm charts for Speedscale BYOC (Bring Your Own Cloud) — capture real traffic with the Speedscale Operator and route it to your own storage backend instead of (or in addition to) Speedscale Cloud.

## Scenarios

| Chart | Stack | Best for |
|---|---|---|
| [`charts/grafana/`](charts/grafana/) | OTel Collector → Loki + Prometheus → Grafana | Live dashboard + PromQL aggregates + LogQL drill-down |
| [`charts/elasticsearch/`](charts/elasticsearch/) | OTel Collector → Elasticsearch → Kibana | Full-text search + Kibana Discover |
| [`charts/fluentbit-gcs/`](charts/fluentbit-gcs/) | OTel Collector → Fluent Bit → GCS | Durable GCS archive + BigQuery |
| [`charts/fluentbit-s3/`](charts/fluentbit-s3/) | OTel Collector → Fluent Bit → S3 | Durable S3 archive + Athena |
| [`charts/otlp/`](charts/otlp/) | OTel Collector → OTLP/HTTP (`otlphttp`) | Any OTLP-native vendor — Dynatrace, Datadog, Honeycomb, New Relic, … |

All scenarios coexist in separate namespaces on the same cluster. Point the Forwarder's `byoc_<backend>` exporter at the backend's collector to choose where traffic goes.

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
  --set 'forwarder.exporters.byoc_grafana.otel_endpoint=http://otel-collector.byoc-grafana.svc.cluster.local:4317'

# Pick a backend — e.g. Grafana + Loki
helm upgrade --install byoc-grafana speedscale-byoc/grafana \
  -n byoc-grafana --create-namespace
```

See each chart's `README.md` for the full install + configure + replay walkthrough.

## Architecture: one backend, one collector, one exporter

Every chart here follows the same rule, and so should any backend you add:

> **One backend = one self-contained chart = one OTel Collector = one Forwarder exporter.**

The Forwarder captures RRPairs and ships them over OTLP to a backend's OTel
Collector. Each chart bundles its **own** Collector (Service on `:4317`) that
exports to **only that backend**. You wire it by pointing one entry in the
Forwarder's `forwarder.exporters` map at that Collector:

```yaml
forwarder:
  exporters:
    byoc_grafana:                    # one named exporter per backend
      otel_endpoint: http://otel-collector.byoc-grafana.svc.cluster.local:4317
      dlp_config_id: standard        # DLP + filtering are PER EXPORTER
      filter_rule: standard
```

Internal Collector fan-out (`exporters: [a, b]`) is reserved for multiple
signals of the **same** backend — e.g. the `grafana` chart's Collector emits
both Loki logs and derived Prometheus metrics. A **different** backend always
gets its own Collector; never add it as a branch on another backend's pipeline.

### Running multiple backends

To send the same traffic to several backends at once, install each chart and
add **one exporter per backend** — each pointed at its own Collector, each with
its own DLP/filter policy:

```yaml
forwarder:
  exporters:
    byoc_grafana:                    # → byoc-grafana       (Loki + Grafana)
      otel_endpoint: http://otel-collector.byoc-grafana.svc.cluster.local:4317
      dlp_config_id: standard
      filter_rule: standard
    byoc_es:                         # → byoc-elasticsearch (Elasticsearch + Kibana)
      otel_endpoint: http://otel-collector.byoc-elasticsearch.svc.cluster.local:4317
      dlp_config_id: standard
      filter_rule: standard
    byoc_s3:                         # → byoc-fluentbit-s3   (S3 archive)
      otel_endpoint: http://otel-collector.byoc-fluentbit-s3.svc.cluster.local:4317
      dlp_config_id: pii-strict      # e.g. archive only heavily-redacted traffic
      filter_rule: http-only
```

Splitting at the Forwarder (rather than fanning out inside one shared Collector)
is deliberate: it gives **per-destination DLP/filtering**, isolates one backend's
failures from another's, and lets you add or remove a backend without touching
the others.

### Adding a new backend

How you add a backend depends on whether it speaks OTLP natively:

**OTLP-native vendor** (Dynatrace, Datadog, Honeycomb, New Relic, …) — do
**not** add a chart. They all share the identical Collector + `otlphttp`
exporter; only the logs endpoint URL and the auth header differ.

1. `helm install byoc-<vendor> speedscale-byoc/otlp` with a values preset
   (endpoint + auth header + token Secret) — see
   [`charts/otlp/examples/`](charts/otlp/examples/).
2. Add one `forwarder.exporters.byoc_<vendor>` entry pointed at that
   Collector's Service, with its own `dlp_config_id` / `filter_rule`.
3. If the vendor isn't already covered, add one `examples/<vendor>.yaml`
   preset to `charts/otlp/` — no template change needed.

**Non-OTLP backend** (object storage, classic Loki/Elasticsearch) — add a
dedicated chart with the appropriate exporter (`awss3` / `loki` /
`elasticsearch`):

1. Add `charts/<backend>/` with an OTel Collector whose pipeline exports
   **only** to that backend (copy the closest existing chart as a template).
2. Add one `forwarder.exporters.byoc_<backend>` entry pointed at the new
   Collector's Service, with its own `dlp_config_id` / `filter_rule`.
3. Do **not** add the backend to an existing Collector's `exporters` list.

Either way the Forwarder wiring is one entry per backend, and backends stay
independent.

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

## Bring your own AI

Once traffic is captured, your data and your model can both stay on your
infrastructure. The [`recipes/`](recipes/) pair proxymock with a **local LLM**
(any OpenAI-compatible server — oMLX, Ollama, vLLM, KServe) for **$0,
zero-egress** workflows. Each is one self-contained script: a deterministic
proxymock spine does the work, and the model is consulted **once**, for the
judgment a script is bad at.

- [`recipes/qa-tester.sh`](recipes/qa-tester.sh) — **regression gate.** Replay
  recorded traffic against a build; proxymock owns pass/fail (exit 0/1); on
  failure the model triages the field-level drift into REGRESSION vs NOISE.
- [`recipes/sre-debug.sh`](recipes/sre-debug.sh) — **incident triage.** Replay
  the failing traffic against a build to reproduce, then the model diagnoses the
  culprit endpoint/dependency, blast radius, and likely root cause.

See [`recipes/README.md`](recipes/README.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
