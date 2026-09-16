# From Datadog to tests and mocks

This recipe starts with one trace in the dedicated Datadog partner organization. The independent Datadog channel sends full Speedscale RRPairs as logs and application telemetry as APM spans. `gather.py` queries both signals by trace ID, converts incoming HTTP traffic into tests, and converts outgoing HTTP traffic into dependency mocks. It does not read GCS or any other storage channel.

## Retrieve a trace

The Datadog application key needs `logs_read_data` and `apm_read`. Set all three partner variables explicitly; the tool never reads `DATADOG_API_KEY`, `DATADOG_APP_KEY`, or a default site.

```sh
export DATADOG_PARTNER_API_KEY='<partner ingest key>'
export DATADOG_PARTNER_APP_KEY='<partner query key>'
export DATADOG_PARTNER_SITE='datadoghq.com'

python3 gather.py \
  --trace-id <32-character-lowercase-trace-id> \
  --service api-gateway \
  --out runs/capture
```

The trace must contain at least one incoming HTTP RRPair, one outgoing HTTP RRPair, and a same-service APM span from the past 24 hours. `provenance.json` records Datadog log IDs, APM span IDs, counts, and retrieval time. Datadog may stringify structured header values; the converter restores their native array form before writing RRPairs.

## Validate the converter

```sh
python3 -m unittest -v test_gather.py
```

The tests cover the Datadog spans request, RRPair payload preservation, header normalization, required incoming/outgoing traffic, output provenance, and rejection of production-style fallback credentials.

## Run the microsvc demo

```sh
python3 demo.py --capture /absolute/path/to/capture \
  --app-dir /absolute/path/to/microsvc \
  --out runs/demo-rehearsal
```

Add `--interactive` to pause before each phase. The runner launches the pinned microsvc API gateway and Redis, serves the captured account-service response with dependency passthrough disabled, then executes baseline, injected HTTP 503, and recovery replays. Every phase checks observed status, body match, and replay exit code.

The captured JWT is re-signed only in the isolated local copy with a fresh gateway secret. Local telemetry export is disabled. `report.html` links to the original partner-account Datadog APM trace and RRPair logs; `summary.json` contains the machine-readable results. Run artifacts are ignored and can contain captured traffic, so review them before sharing.

Cluster deployment belongs in demo-infra, application code belongs in microsvc, and this reusable retrieval/demo workflow belongs in BYOC.
