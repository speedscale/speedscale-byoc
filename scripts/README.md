# BYOC companion scripts

Small, single-file Python tools for gathering RRPair traffic from each BYOC backend and writing a `proxymock`-replayable snapshot directory. All scripts require Python 3.9+ and only use the standard library — no `pip install`.

## `loki-gather.py`

Pull a subset of RRPair traffic from Loki (grafana scenario) and write a `proxymock`-replayable directory.

### Requirements

- Python 3.9+ (stdlib only)
- A reachable Loki HTTP endpoint. With the BYOC reference architecture Loki is `Service: NodePort` on `30031`, so `http://<node-ip>:30031` works without `kubectl port-forward`. On `minikube --driver=docker` on macOS, the node IP is in Docker's hidden VM — use Docker Desktop host networking, a non-docker driver, or `kubectl port-forward svc/loki 30031:3100 -n byoc-grafana`.
- `proxymock` if you want to replay the result

### Usage

```bash
python3 loki-gather.py \
  --loki-url http://<node-ip>:30031 \
  --start    -15m \
  --service  java-server \
  --status   2.. \
  --endpoint '^/spacex/.+' \
  --out-dir  /tmp/spacex-snapshot
```

### Filter flags (translated to LogQL)

| Flag | What it filters |
|---|---|
| `--cluster X` | Loki stream label |
| `--service X` | Loki stream label |
| `--namespace X` | Loki stream label |
| `--method GET` | HTTP method (regex) |
| `--status 2..` | HTTP status (regex) |
| `--endpoint '^/api/.+'` | URL path (regex) |
| `--direction IN\|OUT` | Capture direction |

For full LogQL control, pass `--logql '<your query>'` and the other filter flags are ignored.

### Output shape

```
<out-dir>/
├── .metadata/
│   └── snapshot.json              # id, source=loki, time window, logql
└── snapshot-<uuid>/
    ├── java-server/
    │   ├── <rrpair-uuid>.json
    │   └── ...
    └── <other-host>/
        └── ...
```

Same shape `speedctl proxymock cloud pull snapshot` produces — so anything downstream that reads either source works without modification.

### Known gotchas

- **`body.cluster` workaround.** The script overwrites `body.cluster` with the Loki stream label `cluster` so downstream tools see the right cluster name.
- **Loki cardinality cap (500 series).** The script pipes through `| keep <small-field-list>` after `| json` to avoid blowing past the cap.
- **Port conflicts.** If your gathered set includes Postgres or MySQL recordings, `proxymock mock` will try to bind ports 5432/3306. Filter to HTTP only with `--logql '{...} | json | body_l7protocol="http"'` if needed.

---

## `es-gather.py`

Pull a subset of RRPair traffic from Elasticsearch (elasticsearch scenario). Same CLI shape and output shape as `loki-gather.py`.

### Requirements

- Python 3.9+ (stdlib only)
- A reachable Elasticsearch HTTP endpoint (NodePort `30032` by default)

### Usage

```bash
python3 es-gather.py \
  --es-url   http://<node-ip>:30032 \
  --start    -15m \
  --service  java-server \
  --status   2.. \
  --endpoint '^/spacex/.+' \
  --out-dir  /tmp/spacex-snapshot
```

### Filter flags (translated to ES Query DSL)

| Flag | ES field queried | Match type |
|---|---|---|
| `--cluster X` | `Resource.cluster.keyword` | term (exact) |
| `--service X` | `Attributes.service.keyword` | term (exact) |
| `--namespace X` | `Attributes.namespace.keyword` | term (exact) |
| `--method GET` | `Body.command.keyword` | regexp |
| `--status 2..` | `Body.status.keyword` | regexp |
| `--endpoint '^/api/.+'` | `Body.location.keyword` | regexp |
| `--direction IN\|OUT` | `Body.direction.keyword` | term (exact) |

For full Query DSL control, pass `--query '{"bool":{"must":[...]}}' ` and the other filter flags are ignored.

### Known gotchas

- **`Body.cluster` workaround.** Script overwrites from `Resource.cluster` (always populated correctly).
- **`index.max_result_window` cap (10000 default).** `--limit` defaults to 5000. Narrow the query or use multiple invocations for larger windows.

---

## `gcs-gather.py`

Pull a time window of RRPairs from a GCS bucket (fluentbit scenario) and assemble a proxymock-replayable snapshot.

### Requirements

- Python 3.9+ (stdlib only)
- `gcloud` CLI authenticated (ADC, `gcloud auth login`, or Workload Identity)
- The bucket written to by the fluentbit scenario

### Usage

```bash
python3 gcs-gather.py \
  --bucket   my-rrpair-archive \
  --service  java-server \
  --status   2.. \
  --endpoint '^/spacex/.+' \
  --start    -15m \
  --out-dir  /tmp/spacex-snapshot
```

Pass `--dry-run` to see which GCS partitions the window touches before downloading anything.

### How it works

Enumerates only the Hive partitions (`year=YYYY/month=MM/day=DD/hour=HH/`) that overlap your `--start`/`--end` window — no full-bucket scan. Streams each gzipped NDJSON object, filters in-memory, strips the OTLP envelope, and writes the canonical proxymock snapshot tree.
