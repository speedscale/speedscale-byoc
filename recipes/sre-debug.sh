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
  # proxymock drift fails to marshal its report when a response body carries
  # invalid UTF-8 (e.g. a binary error page) — "DriftSample.value contains
  # invalid UTF-8". Run it on UTF-8-sanitized copies so a bad byte can't kill it.
  local drift srec sobs; drift="$(mktemp)"; srec="$(mktemp -d)"; sobs="$(mktemp -d)"
  python3 - "$1" "$srec" "$2" "$sobs" <<'PY'
import os, glob, sys
for src, dst in ((sys.argv[1], sys.argv[2]), (sys.argv[3], sys.argv[4])):
    for p in glob.glob(os.path.join(src, "**", "*"), recursive=True):
        if os.path.isdir(p): continue
        out = os.path.join(dst, os.path.relpath(p, src))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w", encoding="utf-8").write(open(p, "rb").read().decode("utf-8", "ignore"))
PY
  if ! proxymock drift --source "$srec" --source "$sobs" --sensitivity permissive --out "$drift" >/dev/null 2>&1; then
    echo "FIELD DRIFT: (proxymock drift unavailable)"; rm -rf "$srec" "$sobs" "$drift"; return 0
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
  rm -rf "$srec" "$sobs" "$drift"
}

# status_diff <recorded_dir> <observed_dir> — the unambiguous, DIRECTIONAL signal.
# Reads the response status straight from each side's RRPair files and reports
# how it changed per endpoint: "recorded 200 -> observed 500". Unlike drift's
# unlabeled sample list, this is explicitly sourced, so the diagnosis can't
# invert which way the change went.
status_diff() {  # <recorded_dir> <observed_dir>
  python3 - "$1" "$2" <<'PY'
import os, re, glob, sys, collections
def parse(d):
    out = collections.defaultdict(set)
    for p in glob.glob(os.path.join(d, "**", "*.md"), recursive=True):
        if ".metadata" in p: continue
        try: txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception: continue
        rq = re.search(r'###\s*REQUEST\s*###.*?\n([A-Z]+)\s+(\S+)\s+HTTP/', txt, re.S)
        rs = re.search(r'###\s*RESPONSE\s*###.*?\nHTTP/\S+\s+(\d{3})', txt, re.S)
        if not rq or not rs: continue
        url = re.sub(r'^[a-z]+://[^/]+', '', rq.group(2)).split('?')[0]
        out[(rq.group(1), url)].add(int(rs.group(1)))
    return out
rec, obs = parse(sys.argv[1]), parse(sys.argv[2])
rows = []
for k in sorted(set(rec) | set(obs)):
    r, o = rec.get(k, set()), obs.get(k, set())
    if r != o:
        rows.append(f"{k[0]} {k[1]}: recorded {sorted(r) or '-'} -> observed {sorted(o) or '-'}")
if rows:
    print("STATUS CHANGES (recorded -> observed; observed = the build under investigation):")
    for r in rows: print(" -", r)
else:
    print("STATUS CHANGES: none")
PY
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
status="$(status_diff "$SNAPSHOT" "$run/observed")"
echo "$status"
echo
drift="$(digest_drift "$SNAPSHOT" "$run/observed")"
echo "$drift"
echo
echo ">> diagnosing with local model ..." >&2
cat <<EOF | ask_model "You are a senior SRE triaging an incident. Recorded traffic was replayed against a build under investigation; 'observed' is what that build returned NOW. Ground every claim in the evidence below; do not speculate beyond it. A recorded 2xx that is now 5xx is a live failure in the build — never call that an improvement. Ignore timestamp/Content-Length/date noise."
Recorded traffic replayed against the build. Here is the evidence.

Status changes, directional (recorded -> observed; observed is the build NOW):
$status

Affected endpoints (match summary):
$digest

Field-level drift (supporting detail; samples are not direction-labeled — trust the STATUS CHANGES above for direction):
$drift

Diagnose, briefly:
1. Reproduced? Which endpoints fail and how (cite the recorded -> observed status).
2. Most likely culprit — the single endpoint or downstream dependency.
3. Blast radius — which user-facing flows these endpoints sit on.
4. Most likely root cause and the next diagnostic step.
EOF

exit 1
