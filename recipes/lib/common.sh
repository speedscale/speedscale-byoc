# common.sh — shared helpers for the local-AI recipes. Source, don't run.
#
# The deterministic spine lives here: resolve a snapshot, replay it against a
# target, capture proxymock's JSON metrics, and distill the non-matching
# requests into a compact digest the model can reason over.

RECIPES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASK_GEMMA="$RECIPES_DIR/lib/ask-gemma.sh"

# A snapshot is whatever the scripts/<backend>-gather.py tools wrote, or a
# `speedctl proxymock cloud pull` directory. Default matches the gather
# examples in the top-level README.
SNAPSHOT="${SNAPSHOT:-/tmp/snapshot}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 2; }; }

require_snapshot() {
  if [ ! -d "$SNAPSHOT" ]; then
    cat >&2 <<EOF
error: snapshot directory '$SNAPSHOT' not found.

Gather one first (see the top-level README), e.g.:
  python3 scripts/loki-gather.py --loki-url http://<node-ip>:30031 \\
    --service my-service --start -1h --out-dir $SNAPSHOT
EOF
    exit 2
  fi
}

# replay_snapshot <target> <out_dir> <results_json> [extra proxymock args...]
# Runs the recorded inbound requests against <target>, mocking nothing here —
# downstream dependencies are handled by the caller (see --mock in the recipes).
replay_snapshot() {
  local target="$1" out_dir="$2" results_json="$3"; shift 3
  need proxymock
  proxymock replay \
    --in "$SNAPSHOT" \
    --test-against "$target" \
    --out "$out_dir" \
    --output json \
    "$@" >"$results_json"
}

# digest_failures <results_json> — emit a compact, model-friendly summary of
# what did not match. Parses proxymock replay's `--output json` schema:
#   {"endpoints":[{"url","method","metrics":{"requests.result-match-pct",
#    "requests.failed","requests.total",...}}]}
# The "-ALL-" endpoint holds the run totals; the rest are per url+method.
# Schema-tolerant: if the shape differs we fall back to the raw head so the
# model still has signal.
digest_failures() {
  local results_json="$1"
  python3 - "$results_json" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
try:
    d = json.loads(raw)
except Exception:
    print(raw[:8000]); sys.exit(0)

eps = d.get("endpoints") if isinstance(d, dict) else None
if not isinstance(eps, list):
    print("METRICS: (unrecognized schema — raw head follows)")
    print(raw[:6000]); sys.exit(0)

def m(ep, key, default=None):
    return (ep.get("metrics") or {}).get(key, default)

total = next((e for e in eps if e.get("url") == "-ALL-"), None)
if total:
    print("METRICS:", json.dumps({
        "total":          m(total, "requests.total"),
        "succeeded":      m(total, "requests.succeeded"),
        "failed":         m(total, "requests.failed"),
        "result_match_pct": m(total, "requests.result-match-pct"),
        "latency_p95":    m(total, "latency.p95"),
    }))

fails = []
for e in eps:
    if e.get("url") == "-ALL-":
        continue
    pct = m(e, "requests.result-match-pct", 100)
    failed = m(e, "requests.failed", 0) or 0
    if (pct is not None and pct < 100) or failed > 0:
        fails.append({
            "method": e.get("method"),
            "url": e.get("url"),
            "result_match_pct": pct,
            "failed": failed,
            "total": m(e, "requests.total"),
        })

if fails:
    print(f"NON-MATCHING ({len(fails)}):")
    for h in fails[:50]:
        print(" -", json.dumps(h))
else:
    print("NON-MATCHING: none")
PY
}

# digest_drift <recorded_dir> <observed_dir> — the "what actually differed"
# signal. Runs proxymock's native field-level diff (`proxymock drift`) between
# the recorded golden and the responses observed during replay, then distills
# the DriftReport to: which field changed (json path), on which endpoints, with
# a few short sample values. This is what makes the model's triage trustworthy —
# it sees that http.res.statusCode went 200->500, or that a body field changed,
# not just a match percentage. Date/Content-Length style noise is left in so the
# model can see and discount it.
digest_drift() {
  local recorded="$1" observed="$2"
  need proxymock
  local drift; drift="$(mktemp)"
  if ! proxymock drift --source "$recorded" --source "$observed" \
        --sensitivity permissive --out "$drift" >/dev/null 2>&1; then
    echo "FIELD DRIFT: (proxymock drift unavailable)"; rm -f "$drift"; return 0
  fi
  python3 - "$drift" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("FIELD DRIFT: (no report)"); sys.exit(0)
recs = d.get("recommendations", [])
if not recs:
    print("FIELD DRIFT: none"); sys.exit(0)
print(f"FIELD DRIFT ({len(recs)} field(s) differ; recorded-vs-observed samples, truncated):")
for r in recs[:40]:
    loc = r.get("location")
    eps = ",".join(r.get("endpoints", []))
    vals, seen = [], set()
    for s in r.get("samples", []):
        v = " ".join(str(s.get("value", "")).split())[:80]
        if v and v not in seen:
            seen.add(v); vals.append(v)
        if len(vals) >= 3:
            break
    print(f" - {loc}  [{eps}]")
    for v in vals:
        print(f"     • {v}")
PY
  rm -f "$drift"
}
