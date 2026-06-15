#!/usr/bin/env bash
# debug.sh — replay a window of real captured traffic and let a local model
# triage what broke. The SRE "what is on fire and why" loop, $0 and offline.
#
# Spine (deterministic):  gather window -> replay against the build -> JSON metrics
# Judgment (one model call): classify failures, name the likely culprit dependency
#                            / endpoint, estimate blast radius.
#
# Usage:
#   SNAPSHOT=/tmp/incident debug.sh --test-against http://localhost:8080
#
# Typical pairing: gather the incident window from your BYOC backend first, e.g.
#   python3 scripts/es-gather.py --es-url http://<node>:30032 \
#     --service api-gateway --start -30m --status '5..' --out-dir /tmp/incident
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/../lib/common.sh"

TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --test-against) TARGET="$2"; shift 2;;
    --snapshot)     SNAPSHOT="$2"; shift 2;;
    -h|--help) sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TARGET" ] || { echo "error: --test-against <url> required" >&2; exit 2; }
require_snapshot

run="$(mktemp -d)"; results="$run/results.json"
echo ">> replaying $SNAPSHOT against $TARGET ..." >&2
replay_snapshot "$TARGET" "$run/observed" "$results" || true   # don't abort on replay non-zero; we want to triage it

digest="$(digest_failures "$results")"
echo "$digest"
echo
echo ">> field-level drift (proxymock drift: recorded vs observed) ..." >&2
drift="$(digest_drift "$SNAPSHOT" "$run/observed")"
echo "$drift"
echo
echo ">> triaging with local model ..." >&2

cat <<EOF | "$ASK_GEMMA" "You are a senior SRE. You are handed the result of replaying recorded production traffic against a running build. Use the field-level drift to ground your diagnosis in the exact fields that changed. Diagnose, do not speculate beyond the data."
A replay of recorded production traffic just ran against a build under investigation.

Match summary:
$digest

What actually differed, field by field (recorded vs observed):
$drift

Do three things, briefly:
1. Classify the failures: real regression vs. environmental/noise.
2. Name the single most likely culprit — which downstream dependency or endpoint, and why, citing the URIs above.
3. Estimate blast radius: which user-facing flows these endpoints sit on.
If the digest shows no failures, say so and stop.
EOF
