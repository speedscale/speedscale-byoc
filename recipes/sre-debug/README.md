# SRE debugging

**"Production is throwing errors — what broke and why?"** — without giving anyone
a prod shell.

You already capture traffic into a BYOC backend. When something breaks, gather
the incident window, replay it against a build locally, and let a local model
triage the failures and name the likely culprit. No prod access, no SaaS, no
egress.

## Run it

```bash
# 1. Gather the incident window from your backend (banking app, 5xx, last 30 min)
python3 scripts/es-gather.py \
  --es-url http://<node>:30032 --service api-gateway \
  --status '5..' --start -30m --out-dir /tmp/incident

# 2. Replay + triage against the build you suspect (API gateway on :8080)
SNAPSHOT=/tmp/incident recipes/sre-debug/debug.sh \
  --test-against http://localhost:8080
```

> Example app: the [banking microservices demo](https://github.com/speedscale/microsvc)
> — eight services (`frontend`, `api-gateway`, `user-service`, `accounts-service`,
> `transactions-service`, `fraud-service`, `notification-service`, `ai-service`) over Postgres.
> Generate load with its `simulation-client`, capture to your BYOC backend, then gather as above.

## What it does

1. **Replays** the captured requests against `--test-against` (deterministic;
   proxymock writes observed responses and JSON metrics).
2. **Digests** the non-matching requests, and runs **`proxymock drift`** to list
   the exact fields that changed (status code, body fields) vs. recorded.
3. **Asks the model once** to (a) split real regressions from noise, (b) name
   the single most likely culprit dependency/endpoint with evidence, (c)
   estimate blast radius on user-facing flows.

The model sees only the digest you can also read yourself — it explains the
diff, it does not invent it. If the model server is down you still get the
deterministic digest.

## Knobs

| Var / flag | Meaning |
|---|---|
| `--test-against <url>` | build to replay against (required) |
| `SNAPSHOT` / `--snapshot` | traffic dir (default `/tmp/snapshot`) |
| `QA_MODEL` | model id your server advertises (default `gemma-3-27b-it`) |
| `LLM_BASE_URL` | OpenAI-compatible base (default vLLM `http://localhost:8000/v1`) |
