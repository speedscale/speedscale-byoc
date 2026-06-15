#!/usr/bin/env bash
# qa-tester.sh — the scheduled "$0 QA automation engineer".
#
# A deterministic regression gate: mock the downstream dependencies from the
# snapshot, replay the recorded inbound traffic against the build, and FAIL the
# run on any non-match (proxymock --fail-if). The local model never decides
# pass/fail — proxymock does. The model only triages whatever failed into
# real-regression vs. noise and writes the human-readable summary.
#
# Exit code is the gate: 0 = clean, 1 = regression. Wire it to cron or CI
# exactly like any other test command (see crontab.example, ci-github-actions.example.yml).
#
# Usage:
#   SNAPSHOT=/tmp/snapshot qa-tester.sh --test-against http://localhost:8080 [--mock] [--threshold 100]
#
#   --mock        also stand up `proxymock mock` from the same snapshot so the
#                 build's outbound calls are served offline (route the app's
#                 egress through it per proxymock docs).
#   --threshold N minimum requests.result-match-pct to pass (default 100).
#   --warmup N    fire N throwaway replays first (results ignored) so a cold
#                 SUT/connection-pool blip never counts against the gate
#                 (default 1; use --warmup 0 to disable).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/../lib/common.sh"

TARGET=""; THRESHOLD="100"; DO_MOCK=0; WARMUP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --test-against) TARGET="$2"; shift 2;;
    --snapshot)     SNAPSHOT="$2"; shift 2;;
    --threshold)    THRESHOLD="$2"; shift 2;;
    --mock)         DO_MOCK=1; shift;;
    --warmup)       WARMUP="$2"; shift 2;;
    -h|--help) sed -n '2,24p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TARGET" ] || { echo "error: --test-against <url> required" >&2; exit 2; }
require_snapshot

mock_pid=""
cleanup() { [ -n "$mock_pid" ] && kill "$mock_pid" 2>/dev/null || true; }
trap cleanup EXIT
if [ "$DO_MOCK" -eq 1 ]; then
  need proxymock
  echo ">> starting downstream mock from $SNAPSHOT ..." >&2
  proxymock mock --in "$SNAPSHOT" >/dev/null 2>&1 &
  mock_pid="$!"
  sleep 2
fi

# Warm-up: a flaky SUT can blip (cold connections, JIT, lazy route compile) on
# the first replay after it starts. Fire throwaway passes first so the gate
# measures the build, not the cold start. Results are discarded.
w=0
while [ "$w" -lt "$WARMUP" ]; do
  w=$((w + 1))
  echo ">> warm-up pass $w/$WARMUP (result ignored) ..." >&2
  proxymock replay --in "$SNAPSHOT" --test-against "$TARGET" --no-out --output json >/dev/null 2>&1 || true
done

run="$(mktemp -d)"; results="$run/results.json"
echo ">> regression replay against $TARGET (gate: result-match-pct >= $THRESHOLD) ..." >&2

# proxymock owns the verdict. Capture its exit code; do not let set -e swallow it.
gate_rc=0
replay_snapshot "$TARGET" "$run/observed" "$results" \
  --fail-if "requests.result-match-pct < $THRESHOLD" || gate_rc=$?

digest="$(digest_failures "$results")"
echo "$digest"

if [ "$gate_rc" -eq 0 ]; then
  echo
  echo "PASS — all recorded traffic matched (>= $THRESHOLD%). No model call needed." >&2
  exit 0
fi

echo
echo ">> field-level drift (proxymock drift: recorded vs observed) ..." >&2
drift="$(digest_drift "$SNAPSHOT" "$run/observed")"
echo "$drift"
echo
echo ">> regression detected — triaging with local model ..." >&2
cat <<EOF | "$ASK_GEMMA" "You are a QA engineer triaging a failed regression run. Recorded responses are the golden. Use the field-level drift to separate real regressions (status codes, changed body fields) from environmental noise (timestamps, Content-Length, dates). Be terse."
A scheduled regression replay failed the match-percentage gate.

Match summary:
$digest

What actually differed, field by field (recorded vs observed):
$drift

Produce a short report:
1. Verdict per failing endpoint: REGRESSION or NOISE (and one-line reason).
2. If any REGRESSION: the single highest-priority one to fix first.
3. A one-sentence summary suitable for a ticket title.
EOF

exit 1   # preserve the deterministic gate result for cron/CI
