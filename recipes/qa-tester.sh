#!/usr/bin/env bash
# qa-tester.sh — a $0, offline "QA automation engineer".
#
# Replays recorded traffic against a build and gates pass/fail on whether the
# responses still match the recording — proxymock owns the verdict (exit 0/1).
# On failure, a local LLM triages the field-level drift into REGRESSION vs NOISE.
#
# Self-contained: needs only `proxymock` and any OpenAI-compatible model server
# (oMLX, Ollama, vLLM, KServe, llama.cpp). No SaaS, no subscription, no egress.
#
# Usage:
#   SNAPSHOT=/path/to/snapshot ./qa-tester.sh --test-against http://localhost:3000
#
#   --test-against URL   build to replay against (required)
#   --snapshot DIR       recorded traffic dir (default $SNAPSHOT or /tmp/snapshot).
#                        Any `proxymock record` / `proxymock cloud pull` dir works.
#   --threshold N        min requests.result-match-pct to pass (default 100)
#   --warmup N           throwaway replays before the gate (default 1) so a cold
#                        SUT/connection-pool blip never counts; --warmup 0 disables
#
# Env:
#   LLM_BASE_URL   OpenAI-compatible base (default http://localhost:8000/v1, vLLM).
#                  oMLX: http://127.0.0.1:38010/v1 . Ollama: http://127.0.0.1:11434/v1
#   QA_MODEL       model id your server advertises (default gemma-3-27b-it)
#   QA_LLM_TIMEOUT seconds for the triage call (default 300)
#
# Exit code IS the gate: 0 = clean, 1 = regression. Wire to cron/CI like any test.
set -euo pipefail

TARGET=""; SNAPSHOT="${SNAPSHOT:-/tmp/snapshot}"; THRESHOLD=100; WARMUP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --test-against) TARGET="$2"; shift 2;;
    --snapshot)     SNAPSHOT="$2"; shift 2;;
    --threshold)    THRESHOLD="$2"; shift 2;;
    --warmup)       WARMUP="$2"; shift 2;;
    -h|--help) sed -n '2,26p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TARGET" ] || { echo "error: --test-against <url> required" >&2; exit 2; }
command -v proxymock >/dev/null || { echo "error: proxymock not on PATH" >&2; exit 2; }
[ -d "$SNAPSHOT" ] || { echo "error: snapshot dir '$SNAPSHOT' not found" >&2; exit 2; }

LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
QA_MODEL="${QA_MODEL:-gemma-3-27b-it}"
QA_LLM_TIMEOUT="${QA_LLM_TIMEOUT:-300}"

# One bounded local-model call — prompt on stdin, text on stdout. The ONLY place
# a model is consulted; it never drives tools or decides pass/fail.
ask_model() {
  local system="$1" prompt payload tmp
  prompt="$(cat)"
  payload="$(python3 - "$QA_MODEL" "$system" "$prompt" <<'PY'
import json, sys
print(json.dumps({"model": sys.argv[1], "messages": [
    {"role": "system", "content": sys.argv[2]},
    {"role": "user", "content": sys.argv[3]}],
    "temperature": 0.2, "stream": False}))
PY
)"
  tmp="$(mktemp)"; trap 'rm -f "$tmp"' RETURN
  if ! curl -sf --max-time "$QA_LLM_TIMEOUT" "$LLM_BASE_URL/chat/completions" \
        -H 'Content-Type: application/json' -d "$payload" >"$tmp" 2>/dev/null; then
    echo "(LLM triage skipped — $LLM_BASE_URL unreachable; the deterministic result above stands.)"
    return 0
  fi
  python3 -c "import json; print(json.load(open('$tmp'))['choices'][0]['message']['content'].strip())"
}

# Distill proxymock replay's --output json: run totals + per-endpoint non-matches.
digest_failures() {  # <results.json>
  python3 - "$1" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
try:
    d = json.loads(raw)
except Exception:
    print(raw[:8000]); sys.exit(0)
eps = d.get("endpoints") if isinstance(d, dict) else None
if not isinstance(eps, list):
    print("METRICS: (unrecognized schema — raw head follows)"); print(raw[:6000]); sys.exit(0)
def m(ep, k, dflt=None): return (ep.get("metrics") or {}).get(k, dflt)
total = next((e for e in eps if e.get("url") == "-ALL-"), None)
if total:
    print("METRICS:", json.dumps({
        "total": m(total, "requests.total"), "succeeded": m(total, "requests.succeeded"),
        "failed": m(total, "requests.failed"), "result_match_pct": m(total, "requests.result-match-pct"),
        "latency_p95": m(total, "latency.p95")}))
fails = []
for e in eps:
    if e.get("url") == "-ALL-": continue
    pct = m(e, "requests.result-match-pct", 100); failed = m(e, "requests.failed", 0) or 0
    if (pct is not None and pct < 100) or failed > 0:
        fails.append({"method": e.get("method"), "url": e.get("url"),
                      "result_match_pct": pct, "failed": failed, "total": m(e, "requests.total")})
if fails:
    print(f"NON-MATCHING ({len(fails)}):")
    for h in fails[:50]: print(" -", json.dumps(h))
else:
    print("NON-MATCHING: none")
PY
}

# proxymock drift: the "what actually differed" signal — which field changed
# (json path), on which endpoints, with short samples. Lets the model tell a
# real regression (status code, body field) from noise (timestamps, lengths).
digest_drift() {  # <recorded_dir> <observed_dir>
  local drift; drift="$(mktemp)"
  if ! proxymock drift --source "$1" --source "$2" --sensitivity permissive --out "$drift" >/dev/null 2>&1; then
    echo "FIELD DRIFT: (proxymock drift unavailable)"; rm -f "$drift"; return 0
  fi
  python3 - "$drift" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("FIELD DRIFT: (no report)"); sys.exit(0)
recs = d.get("recommendations", [])
if not recs: print("FIELD DRIFT: none"); sys.exit(0)
print(f"FIELD DRIFT ({len(recs)} field(s) differ; recorded-vs-observed samples, truncated):")
for r in recs[:40]:
    loc = r.get("location"); eps = ",".join(r.get("endpoints", []))
    vals, seen = [], set()
    for s in r.get("samples", []):
        v = " ".join(str(s.get("value", "")).split())[:80]
        if v and v not in seen: seen.add(v); vals.append(v)
        if len(vals) >= 3: break
    print(f" - {loc}  [{eps}]")
    for v in vals: print(f"     • {v}")
PY
  rm -f "$drift"
}

# Warm-up: a SUT can blip on the first replay after it starts (cold connections,
# JIT, lazy route compile). Fire throwaway passes so the gate measures the build,
# not the cold start. Results discarded.
w=0
while [ "$w" -lt "$WARMUP" ]; do
  w=$((w + 1)); echo ">> warm-up pass $w/$WARMUP (result ignored) ..." >&2
  proxymock replay --in "$SNAPSHOT" --test-against "$TARGET" --no-out --output json >/dev/null 2>&1 || true
done

# The gate — proxymock owns pass/fail via --fail-if; exit code is the verdict.
run="$(mktemp -d)"; results="$run/results.json"
echo ">> replay $SNAPSHOT -> $TARGET (gate: result-match-pct >= $THRESHOLD) ..." >&2
gate_rc=0
proxymock replay --in "$SNAPSHOT" --test-against "$TARGET" \
  --out "$run/observed" --output json \
  --fail-if "requests.result-match-pct < $THRESHOLD" >"$results" || gate_rc=$?

digest_failures "$results"

if [ "$gate_rc" -eq 0 ]; then
  echo
  echo "PASS — all recorded traffic matched (>= $THRESHOLD%). No model call needed." >&2
  exit 0
fi

echo
drift="$(digest_drift "$SNAPSHOT" "$run/observed")"
echo "$drift"
echo
echo ">> regression detected — triaging with local model ..." >&2
cat <<EOF | ask_model "You are a QA engineer triaging a failed regression run. Use the field-level drift to separate real regressions (status codes, changed body fields) from environmental noise (timestamps, Content-Length, dates). Be terse."
A scheduled regression replay failed the match-percentage gate.

Match summary:
$(digest_failures "$results")

What actually differed, field by field (recorded vs observed):
$drift

Produce a short report:
1. Verdict per failing endpoint: REGRESSION or NOISE (one-line reason).
2. The single highest-priority regression to fix first.
3. A one-sentence summary suitable for a ticket title.
EOF

exit 1  # preserve the deterministic gate result for cron/CI
