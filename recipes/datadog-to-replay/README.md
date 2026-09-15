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

## Run the demo

After retrieving a capture, run the complete demonstration with one command. Use a successful `GET /api/accounts` trace with one incoming gateway request and one outgoing `banking-accounts` HTTP recording.

```bash
python3 demo.py --capture /absolute/path/to/capture \
  --app-dir /absolute/path/to/microsvc \
  --out runs/demo-rehearsal
```

Add `--interactive` to pause before each phase while presenting. The output directory must be new. Docker Desktop must be running, and ports 18080, 18082, 14140, and 14141 must be free. Python 3.9+, Docker, and `~/.speedscale/proxymock` are required; use `--proxymock` to override its path. The runner launches proxymock from the supplied application checkout and pins the gateway v1.4.128 and Redis images by digest.

The runner starts an isolated gateway and Redis, serves the captured dependency with passthrough disabled, and checks the baseline, injected 503, and recovery in sequence. It requires the expected status, body match, and process exit code for every phase. A failure from another cause stops the demonstration. Temporary containers, network, and mock processes are cleaned up automatically.

Captured JWT claims are re-signed with a fresh secret used only by the local gateway, with a one-hour expiry. This changes authentication in a local test copy, not the original capture or its expected response. This is an isolated demonstration convenience, not a general authentication replay policy. Local telemetry export is disabled; the APM and log links show the original GKE request.

Open `report.html` in the output directory for a compact result page with the original trace ID, Datadog links, and phase results. Select the dedicated partner organization in Datadog. The report labels its execution time and capture retrieval time; it is saved evidence rather than a live dashboard. `summary.json` is the machine-readable result. Logs and replay artifacts remain private in the ignored output directory; share only the reviewed report, which contains no request bodies or authentication headers.

The [presenter guide](DEMO.md) describes a proposed eight-minute walkthrough and the claims this example supports. Keep a rehearsed report available in case Datadog indexing, account access, or network availability interrupts the live segment. Identify it explicitly as the earlier run if used.
