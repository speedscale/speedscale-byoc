#!/usr/bin/env bash
# ask-gemma.sh — the one judgment call.
#
# Reads a prompt on stdin, prints the local model's text answer on stdout.
# This is the ONLY place a model is consulted in any recipe. The model never
# drives tools and never decides control flow — the recipe script does the
# deterministic work (replay, compare, gate) and calls this once for the
# human-language judgment: triage, root-cause guess, repro confirmation.
#
# Speaks only the generic OpenAI-compatible chat API (POST /v1/chat/completions).
# It is deliberately runtime-agnostic — point LLM_BASE_URL at whatever local
# server you run. Prefer a Linux Foundation / CNCF–governed runtime in
# production: vLLM (PyTorch Foundation) or KServe (CNCF). For a laptop, oMLX or
# Ollama expose the same API. No SaaS, no subscription, no egress.
#
# Env:
#   LLM_BASE_URL     OpenAI-compatible base. Default http://localhost:8000/v1
#                    (vLLM's default). oMLX: http://127.0.0.1:38010/v1 .
#                    Ollama: http://127.0.0.1:11434/v1 .
#   QA_MODEL         model id your server advertises (GET /v1/models). Pick a
#                    model big enough to reason over diffs — small ones mislabel
#                    regressions. Examples: gemma-3-27b-it (vLLM/HF),
#                    gemma-4-31b-it-4bit (oMLX), gemma3:12b (Ollama).
#   QA_LLM_TIMEOUT   seconds for the single call (default 300; large local
#                    models are slow on the first, cold call)
#
# Usage:
#   echo "$prompt" | ask-gemma.sh "optional system prompt"
set -euo pipefail

LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
QA_MODEL="${QA_MODEL:-gemma-3-27b-it}"
TIMEOUT="${QA_LLM_TIMEOUT:-300}"
SYSTEM="${1:-You are a precise SRE/QA assistant. Answer in terse, specific prose. Do not invent fields you were not given.}"

PROMPT="$(cat)"

payload="$(python3 - "$QA_MODEL" "$SYSTEM" "$PROMPT" <<'PY'
import json, sys
model, system, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "model": model,
    "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0.2,
    "stream": False,
}))
PY
)"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if ! curl -sf --max-time "$TIMEOUT" "$LLM_BASE_URL/chat/completions" \
      -H 'Content-Type: application/json' -d "$payload" >"$tmp" 2>/dev/null; then
  echo "[ask-gemma] local model unreachable at $LLM_BASE_URL (model '$QA_MODEL')." >&2
  echo "[ask-gemma] Start any OpenAI-compatible server (vLLM, KServe, oMLX, Ollama) and set LLM_BASE_URL." >&2
  echo "(LLM triage skipped — the deterministic proxymock results above stand on their own.)"
  exit 0
fi

python3 -c "import json; print(json.load(open('$tmp'))['choices'][0]['message']['content'].strip())"
