# Speedscale BYOC: Elasticsearch + Kibana

This reference architecture captures real traffic from your apps, ships it through the Speedscale Forwarder to your own Elasticsearch, and lets you slice it through Kibana — then pull any subset back out as a `proxymock`-replayable directory for tests.

Sibling scenario: [`charts/grafana/`](../grafana/) does the same with Loki + Grafana instead. The two coexist on one cluster (separate `byoc-*` namespaces); pick which receives traffic by repointing the Forwarder's `byoc_otel.otel_endpoint`.

## Architecture

**Capture.** The Forwarder's `byoc_otel` exporter ships RRPairs as OTLP logs into your own Elasticsearch via the OTel Collector. Kibana sits on top for indexing, Discover, and ad-hoc queries.

```mermaid
flowchart LR
    apps([Your apps]) --> fwd[Speedscale Forwarder]
    fwd --> col[OTel Collector]
    col --> es[(Elasticsearch)]
    es --> kibana[Kibana]
```

**Replay.** `es-gather.py` queries any subset of Elasticsearch back out and writes a `proxymock`-readable directory matching the same shape as `loki-gather.py`'s output. Same real traffic you captured drives your tests.

```mermaid
flowchart LR
    es[(Elasticsearch)] --> gather[es-gather.py]
    gather --> snap[(Snapshot dir)]
    snap --> pm[proxymock]
    pm <--> test([App under test])
```

## Prerequisites

- A Kubernetes cluster (any flavor — `kind`, `minikube`, EKS, GKE, AKS, k3s)
- `kubectl` configured against it
- `helm` v3
- A Speedscale API key (`SPEEDSCALE_API_KEY`) and your app URL

## Install

Two helm releases: the upstream Speedscale Operator chart (Forwarder + Nettap in the `speedscale` namespace) and this chart (Elasticsearch + Kibana + OTel Collector in `byoc-elasticsearch`).

```bash
helm repo add speedscale https://speedscale.github.io/operator-helm/
helm repo add speedscale-byoc https://speedscale.github.io/speedscale-byoc/
helm repo update

kubectl -n speedscale create secret generic speedscale-airgapped-apikey \
  --from-literal=SPEEDSCALE_API_KEY="<YOUR_API_KEY>" \
  --from-literal=SPEEDSCALE_APP_URL="app.speedscale.com"

# 1. Speedscale Operator + Forwarder, pointing at this scenario's OTel Collector
helm upgrade --install speedscale-operator speedscale/speedscale-operator \
  -n speedscale --create-namespace \
  -f examples/operator-values.yaml

# 2. BYOC backend (ES + Kibana + OTel Collector)
helm upgrade --install byoc-elasticsearch speedscale-byoc/elasticsearch \
  -n byoc-elasticsearch --create-namespace

kubectl -n speedscale get pods
kubectl -n byoc-elasticsearch get pods
```

`values.yaml` documents every overridable knob (NodePorts, ES JVM heap, image versions, logs index name). To customize:

```bash
helm upgrade --install byoc-elasticsearch speedscale-byoc/elasticsearch -n byoc-elasticsearch --create-namespace \
  --set elasticsearch.javaOpts="-Xms1g -Xmx1g" \
  --set logsIndex=my-rrpair-index
```

To inspect rendered manifests: `helm template byoc-elasticsearch speedscale-byoc/elasticsearch -n byoc-elasticsearch`.

## Index + Visualize

ES and Kibana are both `Service: NodePort` (30032 and 30033 respectively) so you don't need a `kubectl port-forward` process babysitting your dev loop. Reach them via the node IP:

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
# or: minikube ip

open "http://${NODE_IP}:30033"                  # Kibana (no auth — xpack.security off)
curl "http://${NODE_IP}:30032/_cat/indices?v"   # ES index list
```

In Kibana → **Discover** → create a data view on the `speedscale-rrpair` index pattern. Useful fields to expose as table columns:

- `Resource.cluster` — your cluster name (use this; `Body.cluster` may read `"undefined"` on some forwarder versions — `Resource.cluster` is always populated correctly)
- `Attributes.service` — the service that produced the RRPair
- `Body.command` / `Body.status` / `Body.location` / `Body.direction` / `Body.duration` — structured request/response fields
- `@timestamp` — ingest time

> **`minikube --driver=docker` on macOS:** the node IP `192.168.49.2` lives inside Docker Desktop's hidden VM and isn't routable from your host. Either flip on Docker Desktop's "host networking" (Settings → Resources → Network), switch to a driver where the VM IP routes natively (OrbStack, vfkit, hyperkit), or run a `socat` container to bridge each port:
> ```bash
> kubectl -n byoc-elasticsearch run es-bridge --image=alpine/socat \
>   --restart=Never -- TCP-LISTEN:30032,fork TCP:localhost:30032
> ```

## Replay (gather a subset of traffic into proxymock)

Once ES has some real traffic, pull any slice of it out as a directory `proxymock` reads:

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')

python3 scripts/es-gather.py \
  --es-url   http://${NODE_IP}:30032 \
  --service  java-server \
  --status   2.. \
  --endpoint '^/spacex/.+' \
  --start    -15m \
  --out-dir  /tmp/spacex-snapshot

proxymock mock --in /tmp/spacex-snapshot
```

The gathered directory is the same shape `loki-gather.py` produces (and the same shape `speedctl proxymock cloud pull snapshot` produces after expanding a cloud snapshot) — so anything in the proxymock ecosystem that reads a recording works without changes. See `scripts/es-gather.py --help` for all filter flags.
