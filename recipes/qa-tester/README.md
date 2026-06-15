# Automated QA tester

**"Gate every build against real recorded traffic, unattended."** The $0 QA
automation engineer: proxymock replays production traffic and owns the pass/fail
verdict; a local model only triages what failed and writes the summary.

## Run it

```bash
SNAPSHOT=/tmp/snapshot recipes/qa-tester/qa-tester.sh \
  --test-against http://localhost:8080 --mock --threshold 100
```

> Example app: the [banking microservices demo](https://github.com/speedscale/microsvc).
> Replay the recorded `api-gateway` traffic against a fresh build; `--mock` serves
> Postgres and inter-service calls from the same snapshot so the run is fully offline.

Exit `0` = clean, `1` = regression. That exit code is the gate — wire it to cron
([crontab.example](crontab.example)) or CI
([ci-github-actions.example.yml](ci-github-actions.example.yml)) like any test.

## What it does

1. **`--mock`** (optional) stands up `proxymock mock` from the snapshot so the
   build's downstream calls are served offline.
2. **Replays** the recorded inbound traffic against the build with
   `--fail-if "requests.result-match-pct < <threshold>"`. **proxymock decides
   pass/fail**, not the model.
3. **On pass**: exits `0`, no model call (cheap and quiet).
4. **On fail**: runs **`proxymock drift`** (recorded golden vs. observed) to get
   the exact fields that changed, then **asks the model once** to label each
   REGRESSION vs. NOISE — grounded in the drift, so a changed status code or body
   field reads as a regression while a timestamp reads as noise — pick the top
   fix, and draft a ticket title; exits `1` preserving the gate.

Because the verdict is deterministic, the model being occasionally wrong about
*why* something failed never lets a regression through or blocks a clean build —
it just makes the triage prose better or worse.

## Knobs

| Flag / var | Meaning |
|---|---|
| `--test-against <url>` | build to gate (required) |
| `--mock` | also serve downstream deps from the snapshot |
| `--threshold N` | min `result-match-pct` to pass (default `100`) |
| `SNAPSHOT` / `--snapshot` | traffic dir (default `/tmp/snapshot`) |
| `QA_MODEL` / `LLM_BASE_URL` | local model id + OpenAI-compatible base (default vLLM `gemma-3-27b-it`) |
