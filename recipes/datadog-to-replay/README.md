# From Datadog to tests and mocks

One banking request can connect four useful artifacts: an APM trace, a searchable capture log, a replay test, and dependency mocks. The application SDK emits the APM spans. Speedscale captures the actual requests and responses. Shared trace IDs connect the two.

The GCS + Datadog partner demo stores payloads in GCS and puts their links in Datadog. Ordinary APM spans and summary logs do not contain enough request/response data to reconstruct faithful tests or mocks. This workflow resolves the Speedscale capture links and uses the full RRPairs as replay inputs and expected responses.

## Retrieve a captured trace

Prerequisites: Python 3, gcloud with access to the demo bucket, and the partner Datadog API/app keys. The app key needs logs_read_data and apm_read. Set DATADOG_PARTNER_API_KEY, DATADOG_PARTNER_APP_KEY, and DATADOG_PARTNER_SITE explicitly; the tool never falls back to production credentials.

Select an api-gateway trace containing a successful account request in the partner Datadog account. Copy its 32-character hexadecimal trace ID. The log and APM span must both be searchable within the last 24 hours.

```bash
python3 gather.py --trace-id <trace-id> \
  --bucket <capture-bucket> --gcloud-account <reader-account> \
  --out runs/<trace-id>
```

The tool verifies the same-service APM trace and capture log, checks the bucket/service/trace in the GCS link, downloads the archived records, and verifies each record's trace ID. Incoming HTTP records become `tests/`; outgoing HTTP records become `mocks/`. `provenance.json` records the Datadog log IDs, APM spans, source GCS objects, and retrieval time. A trace without both incoming and outgoing HTTP records is rejected.

Recordings stay in the ignored `runs/` directory with restricted file permissions. They retain captured payloads and authentication headers, so keep them local and review/redact them before sharing. No recording is committed by this demo.

## Demonstration sequence

1. Show the selected banking request in Datadog APM and the matching Speedscale capture log.
2. Run the retrieval command. Inspect the actual request, expected response, and outgoing dependency recording.
3. Run the unchanged microsvc API gateway locally with Redis. Serve the captured account-service response through proxymock with passthrough disabled, and route the local gateway's account-service URL to that mock. Refresh the captured JWT for the isolated demo key if it has expired; preserve the recorded response assertions.
4. Replay the incoming request into that local gateway. Require `requests.result-match-pct == 100` as the automated test gate.
5. Inject a dependency HTTP 503 with proxymock's chaos option and replay the same test. The changed response should fail that gate. Remove the fault and rerun to show recovery.

The September 15, 2026 live run completed all five steps for `GET /api/accounts`: baseline HTTP 200 passed with an exact body match, injected HTTP 503 failed with exit code 1, and recovery HTTP 200 passed. One incoming request and one outgoing dependency record were retrieved through matching Datadog APM/capture logs and GCS. The captured JWT was still valid; no recording or assertion was modified. This qualifies one HTTP gateway scenario using the existing development capture harness, not a public-release deployment or every microsvc workflow.

The captured response provides a regression baseline, not an independent claim that the original behavior was correct. Add explicit business assertions when the desired behavior differs from what was recorded. Stateful dependencies and calls without propagated trace context can require a broader capture window; a trace alone does not guarantee a complete isolated test environment.

## Validate the retrieval tool

```bash
python3 -m unittest -v test_gather.py
```

The tests cover source-link validation, mixed-trace rejection, payload/header preservation, and conversion into separate test/mock files with provenance. They use synthetic fixtures and make no external requests.

This recipe belongs with the BYOC capture and retrieval workflow. Cluster-specific deployment configuration lives in demo-infra; the application remains in microsvc. Unlike `scripts/datadog-gather.py`, which reads full RRPair bodies from Datadog, this recipe resolves GCS links from correlation logs.

## Run the local gateway demonstration

Use a captured successful `GET /api/accounts` with an outgoing `banking-accounts` HTTP recording. These commands were verified on Docker Desktop with `ghcr.io/speedscale/microsvc/api-gateway:v1.4.128`. Run proxymock from the microsvc application checkout. Set `CAPTURE` to the absolute retrieval output directory and `PROXYMOCK` to the executable in each terminal. Output directories must be new for each run.

In the first terminal, serve the captured dependency:

```bash
export CAPTURE=/absolute/path/to/capture
export PROXYMOCK="$HOME/.speedscale/proxymock"
"$PROXYMOCK" mock --in "$CAPTURE/mocks" --out "$CAPTURE/mock-baseline" \
  --no-passthrough --map 18082=http://banking-accounts:80 \
  --proxy-out-port 14140 --health-port 14141
```

In the second terminal, start the gateway and Redis:

```bash
docker network create dd-replay-validation
docker run -d --name dd-replay-redis --network dd-replay-validation redis:7-alpine
docker run -d --name dd-replay-gateway --network dd-replay-validation \
  --add-host banking-accounts:host-gateway -p 127.0.0.1:18080:8080 \
  -e REDIS_HOST=dd-replay-redis \
  -e ACCOUNTS_SERVICE_URL=http://banking-accounts:18082 \
  -e OTEL_SDK_DISABLED=true -e OTEL_TRACES_EXPORTER=none \
  -e OTEL_METRICS_EXPORTER=none -e OTEL_LOGS_EXPORTER=none \
  -e LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_CLOUD_GATEWAY=INFO \
  --entrypoint /bin/sh ghcr.io/speedscale/microsvc/api-gateway:v1.4.128 \
  -c 'exec java -Xms64m -Xmx128m -XX:MaxMetaspaceSize=128m -XX:+UseSerialGC org.springframework.boot.loader.launch.JarLauncher'
```

Wait for the gateway to listen on port 18080. Preserve `banking-accounts` in the URL: substituting `host.docker.internal` changes the mock signature and produces a fail-closed 404. Redis runs locally; no live account service is used. This gateway-only replay disables local telemetry export; the demonstrated APM trace is the original GKE request.

Run the baseline test:

```bash
"$PROXYMOCK" replay --in "$CAPTURE/tests" --test-against http://localhost:18080 \
  --out "$CAPTURE/replay-baseline" --fail-if 'requests.result-match-pct != 100'
echo $?
```

Expect exit 0 and a passing `replay-verdict.json`, including `bodyMatch: pass`. Stop the mock with Ctrl-C, restart the same mock command with `--chaos '*:status=503'` and `--out "$CAPTURE/mock-fault"`, then replay into `--out "$CAPTURE/replay-fault"`. Expect exit 1, recorded status 200, and observed status 503. Stop the faulted mock, restart without chaos using a new output directory, and replay into `--out "$CAPTURE/replay-recovery"`; expect exit 0 again. Check the verdict's status codes so an unrelated failure cannot masquerade as the intended fault.

Stop the mock and remove only these demo resources afterward:

```bash
docker rm -f dd-replay-gateway dd-replay-redis
docker network rm dd-replay-validation
```
