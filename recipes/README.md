# Recipes: bring your own AI

Pair **proxymock** (record / replay, free and local) with a **local LLM** to do
real QA work at **$0 subscription and zero egress** — your traffic and your model
both stay on your infrastructure, which is the whole point of BYOC.

## `qa-tester.sh` — the $0 QA automation engineer

Replay recorded traffic against a build; **proxymock owns pass/fail** (exit 0/1);
on failure a **local model** triages the field-level drift into REGRESSION vs
NOISE. One self-contained script — copy it and run.

```bash
# 1. get a snapshot of real traffic (either works)
proxymock record --app-port 3000            # capture locally, or…
proxymock cloud pull snapshot <id>          # …pull a recorded one

# 2. gate a build against it
SNAPSHOT=proxymock/recorded-… ./qa-tester.sh --test-against http://localhost:3000
```

Exit `0` = clean, `1` = regression — wire it to cron or CI like any test command.

### How it works

1. **Warm-up** — a throwaway replay first, so a cold-start blip doesn't count.
2. **Replay** the recorded requests with `--fail-if result-match-pct < N`. proxymock
   decides pass/fail; the model is never in that path.
3. **On pass** — exit 0, no model call (cheap and quiet).
4. **On fail** — `proxymock drift` extracts the exact fields that changed, then the
   model labels each REGRESSION vs NOISE, picks the top fix, drafts a ticket title.

### Requirements

- **proxymock** — record / replay / drift, local and free.
- **An OpenAI-compatible model server** — set `LLM_BASE_URL` + `QA_MODEL`. Default is
  vLLM's `http://localhost:8000/v1`; oMLX and Ollama expose the same API. Prefer a
  Linux-Foundation runtime (vLLM, KServe) in production; OpenTelemetry (CNCF) is the
  same transport the BYOC charts already use. If the server is down, the gate still
  runs and just skips the triage prose.

### Notes / knobs

| flag / env | meaning |
|---|---|
| `--test-against URL` | build to gate (required) |
| `--snapshot DIR` / `SNAPSHOT` | recorded traffic (default `/tmp/snapshot`) |
| `--threshold N` | min `result-match-pct` to pass (default 100) |
| `--warmup N` | throwaway replays before the gate (default 1) |
| `QA_MODEL` / `LLM_BASE_URL` | local model id + OpenAI-compatible base |

**Auth:** if the recorded requests carry tokens that expire (or are DLP-redacted),
do a fresh login first and substitute the token into the `Authorization` header
before replay — otherwise auth'd endpoints will fail on a stale credential, not a
real regression.
