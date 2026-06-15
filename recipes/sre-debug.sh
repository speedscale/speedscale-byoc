#!/usr/bin/env bash
# sre-debug.sh — a $0, offline SRE incident triage tool.
#
# Replay a window of recorded production traffic against a build under
# investigation, then have a local LLM DIAGNOSE what's wrong — which endpoint or
# dependency is the culprit, the blast radius, and a root-cause hypothesis —
# grounded in proxymock's field-level drift. No prod access, no SaaS, no egress.
#
# This is the investigative cousin of qa-tester.sh: instead of a pass/fail gate,
# it produces a diagnosis. Point it at the build you suspect and the traffic that
# exercises the failing path.
#
# Get the traffic first — e.g. the last 30 min of 5xx for a service:
#   proxymock cloud search <service> --from now-30m --filter-query '(status IS "500")'
#   proxymock cloud pull snapshot <id>     # or pull via the proxymock MCP
#
# Usage:
#   SNAPSHOT=/path/to/incident ./sre-debug.sh --test-against http://localhost:3000
#
#   --test-against URL   build to replay against (required)
#   --snapshot DIR       recorded traffic dir (default $SNAPSHOT or /tmp/incident)
#   --warmup N           throwaway replays before measuring (default 1) so a cold
#                        SUT blip isn't mistaken for the incident; --warmup 0 off
#
# Env:
#   LLM_BASE_URL   OpenAI-compatible base (default http://localhost:8000/v1, vLLM).
#                  oMLX: http://127.0.0.1:38010/v1 . Ollama: http://127.0.0.1:11434/v1
#   QA_MODEL       model id your server advertises (default gemma-3-27b-it)
#   QA_LLM_TIMEOUT seconds for the diagnosis call (default 300)
#
# Exit 0 = nothing reproduced (traffic all matched — likely transient/env);
# exit 1 = failures reproduced (a diagnosis is printed).
set -euo pipefail

TARGET=""; SNAPSHOT="${SNAPSHOT:-/tmp/incident}"; WARMUP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --test-against) TARGET="$2"; shift 2;;
    --snapshot)     SNAPSHOT="$2"; shift 2;;
    --warmup)       WARMUP="$2"; shift 2;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TARGET" ] || { echo "error: --test-against <url> required" >&2; exit 2; }
command -v proxymock >/dev/null || { echo "error: proxymock not on PATH" >&2; exit 2; }
[ -d "$SNAPSHOT" ] || { echo "error: snapshot dir '$SNAPSHOT' not found" >&2; exit 2; }

LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
QA_MODEL="${QA_MODEL:-gemma-3-27b-it}"
QA_LLM_TIMEOUT="${QA_LLM_TIMEOUT:-300}"

# One bounded local-model call — prompt on stdin, text on stdout.
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
    echo "(LLM diagnosis skipped — $LLM_BASE_URL unreachable; the drift above is the raw evidence.)"
    return 0
  fi
  python3 -c "import json; print(json.load(open('$tmp'))['choices'][0]['message']['content'].strip())"
}

# Distill proxymock replay's --output json: run totals + per-endpoint failures.
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
    print(f"AFFECTED ENDPOINTS ({len(fails)}):")
    for h in fails[:50]: print(" -", json.dumps(h))
else:
    print("AFFECTED ENDPOINTS: none (traffic matched — not reproduced)")
PY
}

# proxymock drift: the field-level "what differed" — status codes, body fields,
# and (if the snapshot captured them) the downstream dependency responses.
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

# Warm-up so a cold-start blip isn't mistaken for the incident. Results ignored.
w=0
while [ "$w" -lt "$WARMUP" ]; do
  w=$((w + 1)); echo ">> warm-up pass $w/$WARMUP (ignored) ..." >&2
  proxymock replay --in "$SNAPSHOT" --test-against "$TARGET" --no-out --output json >/dev/null 2>&1 || true
done

run="$(mktemp -d)"; results="$run/results.json"
echo ">> replaying $SNAPSHOT against $TARGET to reproduce ..." >&2
rc=0
proxymock replay --in "$SNAPSHOT" --test-against "$TARGET" \
  --out "$run/observed" --output json \
  --fail-if "requests.result-match-pct < 100" >"$results" || rc=$?

digest="$(digest_failures "$results")"
echo "$digest"

if [ "$rc" -eq 0 ]; then
  echo
  echo "Nothing reproduced — replayed traffic matched the recording. Likely transient or environmental." >&2
  exit 0
fi

echo
drift="$(digest_drift "$SNAPSHOT" "$run/observed")"
echo "$drift"
echo
echo ">> diagnosing with local model ..." >&2
cat <<EOF | ask_model "You are a senior SRE triaging an incident. Recorded traffic was replayed against a build under investigation. Ground every claim in the match summary and field drift below; do not speculate beyond them. Ignore timestamp/Content-Length/date noise."
Recorded traffic replayed against the build. Here is the evidence.

Affected endpoints (match summary):
$digest

What actually differed, field by field (recorded vs observed):
$drift

Diagnose, briefly:
1. Reproduced? Which endpoints fail, and how (status / body field).
2. Most likely culprit — the single endpoint or downstream dependency, citing the drift.
3. Blast radius — which user-facing flows these endpoints sit on.
4. Most likely root cause and the next diagnostic step.
EOF

exit 1
