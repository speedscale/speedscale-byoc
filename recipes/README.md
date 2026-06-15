# Recipes: bring your own AI

These recipes pair **proxymock** (record / mock / replay, free and local) with a
**local LLM** (Gemma 4 works well) to do real QA and SRE work at **$0
subscription and zero network egress**. Your traffic and your model both stay on
your infrastructure — which is the whole point of BYOC.

## Open source & governance

The recipes deliberately depend only on **open standards and Linux Foundation /
CNCF–governed projects**, so nothing here ties you to a vendor:

- **Model serving** — the model is reached over the generic **OpenAI-compatible
  chat API** (`POST /v1/chat/completions`). Run it on a Linux-Foundation runtime:
  [**vLLM**](https://docs.vllm.ai) (PyTorch Foundation) or [**KServe**](https://kserve.github.io/website/)
  (CNCF) for in-cluster serving. On a laptop, oMLX or [Ollama](https://ollama.com)
  expose the same API — they're dev conveniences, not the production target.
- **The pipeline you already run** — capture flows over **OpenTelemetry** (CNCF),
  the same transport the BYOC charts use.
- **Models** — Gemma is open-weight; swap in any instruct model your runtime
  serves. Nothing in the recipe is model-specific beyond `QA_MODEL`.

Point `LLM_BASE_URL` at your server and set `QA_MODEL` to whatever it advertises
(`GET /v1/models`). Default is vLLM's `http://localhost:8000/v1`.

## The shape of every recipe

A deterministic script does the work; the model is consulted **once**, for the
one judgment a script is bad at. The model never drives tools and never decides
pass/fail — proxymock does, via exit codes and `--fail-if`. This is deliberately
the opposite of an autonomous agent: it is robust on local models because the
hard part (orchestration) is plain shell, and the model only has to write good
prose about a diff it is handed.

```
   proxymock (deterministic)                 local model (one call)
   ─────────────────────────                 ──────────────────────
   gather → mock → replay → drift    ──────►  triage / root-cause / confirm
        │                                              │
        └── exit code = the gate                       └── human-language judgment
```

## The recipes

| Recipe | Question it answers | Entry point |
|---|---|---|
| [`sre-debug/`](sre-debug/) | "Production is throwing errors — what broke and why?" | `debug.sh` |
| [`reproduce/`](reproduce/) | "Here's a vague bug report — reproduce it deterministically." | `reproduce.sh` |
| [`qa-tester/`](qa-tester/) | "Gate every build against real recorded traffic, unattended." | `qa-tester.sh` |

## Prerequisites

- **proxymock** — `record`, `mock`, `replay` run locally for free. See the
  [docs](https://docs.speedscale.com/proxymock/).
- **A snapshot of traffic** — any `scripts/<backend>-gather.py` output, or a
  `speedctl proxymock cloud pull` directory. Recipes read it via `SNAPSHOT` (or
  `--snapshot`), default `/tmp/snapshot`. Need an app to try it on? The
  [banking microservices demo](https://github.com/speedscale/microsvc) is eight
  services — `frontend`, `api-gateway`, `user-service`, `accounts-service`,
  `transactions-service`, `fraud-service`, `notification-service`, `ai-service`
  — and ships a `simulation-client` that drives realistic traffic
  (`/api/users/login`, `/api/accounts`, `/api/transactions/transfer`,
  `/api/chat`, …). Capture that to your BYOC backend and gather from it.
- **An OpenAI-compatible local model server** (see *Open source & governance*).
  Set `LLM_BASE_URL` (default vLLM `http://localhost:8000/v1`) and `QA_MODEL` to
  whatever your server advertises. Use a model big enough to reason over diffs —
  small variants mislabel regressions. If the server is down, recipes still run
  and print the deterministic result — they just skip the triage prose.

## Shared bits

- [`lib/ask-gemma.sh`](lib/ask-gemma.sh) — the single bounded model call (stdin → text, OpenAI `/v1/chat/completions`).
- [`lib/common.sh`](lib/common.sh) — snapshot resolution, replay, match digest, and the
  field-level diff via **`proxymock drift`** (recorded golden vs. observed replay).
  Drift is what makes triage trustworthy: the model sees *which field changed*
  (`http.res.statusCode` 200→302, a body field, …) and can tell a real regression
  from timestamp/Content-Length noise — not just a match percentage.
