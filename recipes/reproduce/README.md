# Reproduce a bug from real traffic

**"Here's a vague bug report — reproduce it deterministically."**

A report says "a transfer sometimes returns the wrong balance." Instead of
guessing inputs, pull the *actual* recorded request and replay just that one
against a local build. The recorded response is the golden; the model confirms
whether the build still matches it and states the exact delta.

## Run it

```bash
SNAPSHOT=/tmp/snapshot recipes/reproduce/reproduce.sh \
  --filter /api/transactions/transfer \
  --test-against http://localhost:8080
```

> Example app: the [banking microservices demo](https://github.com/speedscale/microsvc).
> Other handy filters: `/api/users/login`, `/api/accounts`, `/api/transactions/withdraw`.

`--filter` is a substring of the recorded request URI. The script copies only
the matching RRPairs into a temp snapshot and replays those — so the repro is
the suspect call and nothing else.

## What it does

1. **Narrows** the snapshot to requests whose recorded URI contains `--filter`.
2. **Replays** that subset against `--test-against` (deterministic).
3. **Compares** observed vs. recorded with **`proxymock drift`**, surfacing the
   exact field/path that differs (and ignoring timestamp noise).
4. **Asks the model once**: `REPRODUCED` / `NOT-REPRODUCED` plus the precise
   field or status that differs.

A clean, attachable repro: the exact request, the golden response, the observed
response, and a one-line verdict — straight from production traffic, no
synthetic test to argue about.

## Knobs

| Var / flag | Meaning |
|---|---|
| `--filter <uri-substr>` | which recorded request to reproduce (required) |
| `--test-against <url>` | build to replay against (required) |
| `SNAPSHOT` / `--snapshot` | traffic dir (default `/tmp/snapshot`) |
| `QA_MODEL` / `LLM_BASE_URL` | local model id + OpenAI-compatible base (default vLLM `gemma-3-27b-it`) |
