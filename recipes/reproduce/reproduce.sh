#!/usr/bin/env bash
# reproduce.sh — turn a vague bug report into a deterministic, one-command repro
# from real traffic. Replays just the offending request(s) against a local build
# and asks a local model to confirm reproduced / not-reproduced and describe the
# delta from the recorded golden.
#
# Spine (deterministic):  filter snapshot to the suspect requests -> replay ->
#                         compare observed vs recorded response
# Judgment (one model call): reproduced? what is the precise delta?
#
# Usage:
#   SNAPSHOT=/tmp/snapshot reproduce.sh --filter /api/transactions/transfer --test-against http://localhost:8080
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/../lib/common.sh"

TARGET=""; FILTER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --test-against) TARGET="$2"; shift 2;;
    --filter)       FILTER="$2"; shift 2;;
    --snapshot)     SNAPSHOT="$2"; shift 2;;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TARGET" ] || { echo "error: --test-against <url> required" >&2; exit 2; }
[ -n "$FILTER" ] || { echo "error: --filter <uri-substring> required (the request that misbehaves)" >&2; exit 2; }
require_snapshot

# Narrow the snapshot to just the RRPairs whose recorded request URI matches the
# filter, so the repro replays the suspect call and nothing else.
subset="$(mktemp -d)"
match_count="$(python3 - "$SNAPSHOT" "$subset" "$FILTER" <<'PY'
import json, os, re, shutil, sys
src, dst, needle = sys.argv[1], sys.argv[2], sys.argv[3]

def uri_of(p):
    # proxymock snapshots come in two shapes: .json (gather scripts / cloud
    # pull) and .md (CLI `proxymock record`). Support both.
    if p.endswith(".json"):
        try:
            return json.load(open(p)).get("http", {}).get("req", {}).get("uri", "")
        except Exception:
            return ""
    if p.endswith(".md"):
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            return ""
        m = re.search(r'###\s*REQUEST\s*###.*?\n```\n[A-Z]+\s+(\S+)\s+HTTP/', txt, re.S)
        return m.group(1) if m else ""
    return ""

n = 0
for root, _, files in os.walk(src):
    for f in files:
        if not (f.endswith(".json") or f.endswith(".md")):
            continue
        p = os.path.join(root, f)
        if needle in uri_of(p):
            rel = os.path.relpath(root, src)
            od = os.path.join(dst, rel); os.makedirs(od, exist_ok=True)
            shutil.copy(p, od); n += 1
print(n)
PY
)"
if [ "$match_count" -eq 0 ]; then
  echo "error: no recorded request URI contains '$FILTER' in $SNAPSHOT" >&2; exit 1
fi
echo ">> reproducing $match_count request(s) matching '$FILTER' against $TARGET ..." >&2

run="$(mktemp -d)"; results="$run/results.json"
SNAPSHOT="$subset" replay_snapshot "$TARGET" "$run/observed" "$results" || true
digest="$(digest_failures "$results")"
echo "$digest"
echo
echo ">> field-level drift (proxymock drift: recorded vs observed) ..." >&2
drift="$(digest_drift "$subset" "$run/observed")"
echo "$drift"
echo
echo ">> confirming with local model ..." >&2

cat <<EOF | "$ASK_GEMMA" "You confirm bug reproductions. You are given a replay of a single recorded request against a fresh build. Recorded response = the golden. Use the field-level drift to name the exact delta. Ignore timestamp/date/content-length noise."
A bug report points at requests matching: '$FILTER'
Those $match_count request(s) were just replayed against the build.

Match summary:
$digest

What actually differed, field by field (recorded vs observed):
$drift

Answer in two lines:
1. REPRODUCED or NOT-REPRODUCED (one word), based on whether a meaningful field (status code or body content, not timestamps) diverged from the recorded golden.
2. The precise delta: the field/path and the recorded-vs-observed values. If NOT-REPRODUCED, say what matched.
EOF
