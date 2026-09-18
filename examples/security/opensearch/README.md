# OpenSearch Security Analytics demo

This local demo sends Speedscale-shaped OTLP logs through the same metadata-only normalization used by the generic OTLP chart, stores them in OpenSearch through Data Prepper, and detects authentication failures with an OpenSearch Security Analytics Sigma rule. The captured request and response bodies are deliberately removed before export.

## Run the demo

Requirements: Docker Compose, `curl`, and Python 3. OpenSearch needs at least 4 GB of Docker memory.

```sh
cd examples/security/opensearch
./demo.sh
```

The script starts OpenSearch 3.6, OpenSearch Dashboards, Data Prepper 2.16, and the OpenTelemetry Collector. It sends a synthetic RRPair containing a marker secret, verifies that normalized security fields reached OpenSearch without that secret, then creates and enables a Security Analytics detector.

Open <http://localhost:5601>, sign in with `admin` and `Speedscale-OpenSearch-Demo-2026!`, and choose **OpenSearch Plugins > Security Analytics > Findings**. The detector runs once per minute, so the first finding can take about a minute to appear.

Query the indexed events directly:

```sh
curl --insecure --user 'admin:Speedscale-OpenSearch-Demo-2026!' \
  'https://localhost:9200/speedscale-security-*/_search?pretty' \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"http.response.status_code":401}}}'
```

Stop the services without deleting their data:

```sh
docker compose down
```

To reset this demo only, including its OpenSearch volume, run `docker compose down --volumes` from this directory.

## Use the Helm chart instead of the demo collector

The local collector mirrors `charts/otlp` so the demo is self-contained. For a real cluster, expose Data Prepper with TLS and authentication, create the token Secret, and install the generic OTLP chart with [`charts/otlp/examples/opensearch-security.yaml`](../../../charts/otlp/examples/opensearch-security.yaml). Keep `securityProfile.includeFullPayload: false` unless the security team has explicitly approved captured payload storage.

Point a dedicated Speedscale Forwarder exporter at that chart release so security filters, DLP settings, credentials, and delivery failures remain isolated from observability exports.
