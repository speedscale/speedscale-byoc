#!/usr/bin/env python3
"""loki-gather.py — pull a subset of BYOC RRPair traffic from Loki and write a
proxymock-replayable directory.

This is the "gather" half of the Speedscale Cloud Create-Snapshot flow, sourced
from Loki instead of Speedscale's S3 snapshot store. Once this script writes
the directory, `proxymock mock --in <dir>` (or `proxymock replay`) serves the
recorded responses — analysis is proxymock's job, not this script's.

Pattern intent: companion scripts live next to the reference architecture they
enable. Today this script lives in the BYOC + Grafana reference arch under
demos/. Once we accumulate a handful (loki-gather, elasticsearch-gather,
fluent-bit-gather…) they get promoted to their own repo with proper packaging.
For now: stdlib only, no `pip install`, easy to fork.

Usage:

    python3 loki-gather.py \\
      --loki-url http://localhost:38030 \\
      --service  java-server \\
      --status   2.. \\
      --endpoint '^/spacex/.+' \\
      --start    -15m \\
      --out-dir  /tmp/spacex-snapshot

    proxymock mock --in /tmp/spacex-snapshot --listen :8080

Power-user mode bypasses the flag translation:

    python3 loki-gather.py \\
      --loki-url http://localhost:38030 \\
      --logql    '{service="java-server"} | json | body_status=~"2.."' \\
      --start    -15m \\
      --out-dir  /tmp/x

Reference: see Linear S-11101 for the design + Loki→RRPair conversion table.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ─── time parsing ───────────────────────────────────────────────────────────


def parse_time(s: str, *, now: datetime | None = None) -> datetime:
    """Accept 'now', a relative offset like '-15m' / '-1h' / '-2d', or RFC3339.

    Everything else raises argparse-friendly ValueError.
    """
    now = now or datetime.now(timezone.utc)
    s = s.strip()
    if s in ("now", ""):
        return now
    m = re.fullmatch(r"-(\d+)([smhd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return now - delta
    # try parsing as RFC3339; accept "Z" or +00:00 forms
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"can't parse time {s!r} — use 'now', '-15m'/'-2h'/'-1d', or RFC3339")


# ─── LogQL construction ─────────────────────────────────────────────────────


# Body fields we parse out of the JSON log line so the LogQL filter stages
# (`| body_status=~...`, `| body_direction=...`) can act on them server-side.
#
# This is a FILTER-ONLY concern. We deliberately do NOT `| keep` these fields:
# `keep` prunes the extracted label set, and on some Loki configurations it
# also rewrites the returned log line down to just the kept fields — which
# silently drops the rest of the RRPair (the full `http` req/res with headers
# and bodies). The written RRPair must be the COMPLETE log line value, since
# the Loki log line literally IS the full RRPair JSON the forwarder emitted.
# So `| json` extracts the body_* fields for filtering, the filter stages run
# server-side, and `query_loki` writes the untouched original line.
BODY_FILTER_FIELDS = [
    "body_command", "body_status", "body_location", "body_direction",
]


def build_logql(args: argparse.Namespace) -> str:
    """Translate the human-friendly CLI flags into a single LogQL query string.

    Stream-label selectors (cluster/service/namespace) go inside the {…} matcher
    so Loki uses its index — much cheaper than post-filter. Body fields require
    `| json` parsing and then post-filter stages. The `| json` parse is used for
    server-side FILTERING only; we never `| keep`, so the returned log line stays
    the full RRPair (see BODY_FILTER_FIELDS).
    """
    if args.logql:
        return args.logql

    stream: list[str] = []
    for label, val in (("cluster", args.cluster), ("service", args.service), ("namespace", args.namespace)):
        if val:
            stream.append(f'{label}="{val}"')
    # Loki requires at least one stream matcher; default to "any service" so
    # the user can pass only body-level filters if they want.
    if not stream:
        stream.append('service=~".+"')

    pipeline = ["| json"]

    if args.method:
        pipeline.append(f'| body_command=~"{args.method}"')
    if args.status:
        pipeline.append(f'| body_status=~"{args.status}"')
    if args.endpoint:
        pipeline.append(f'| body_location=~"{args.endpoint}"')
    if args.direction:
        pipeline.append(f'| body_direction="{args.direction}"')

    return "{" + ", ".join(stream) + "} " + " ".join(pipeline)


# ─── Loki query ─────────────────────────────────────────────────────────────


# Loki caps a single query_range gRPC response at ~4 MB (the inter-component
# `grpc_server_max_recv_msg_size`, default 4194304). A full RRPair carries the
# entire http req/res with headers + bodies, so as few as ~30 of them blow the
# cap and Loki replies `HTTP 500 ... ResourceExhausted ... larger than max`.
# A single query therefore can't return a large extract.
#
# We page with a timestamp cursor: each call asks for at most _PAGE_LINES log
# lines (direction=backward, newest first), then the next call moves `end` to
# just before the oldest line we got, until we have `limit` records or the
# window is exhausted. _PAGE_LINES is small enough that a page of full RRPairs
# stays well under the cap; if a page still trips it (unusually fat RRPairs),
# we halve the page size for that call and retry.
_RESOURCE_EXHAUSTED = "ResourceExhausted"

# Lines per page. ~25 full RRPairs is comfortably under 4 MB in practice; small
# enough to be safe, large enough to keep the call count reasonable.
_PAGE_LINES = 25


def _rrpair_id(body: dict) -> str:
    """Stable identity for dedup across page boundaries. RRPairs carry a `uuid`
    (and `body_uuid` in the parsed form); fall back to a structural digest so a
    missing uuid never collapses distinct records.
    """
    for k in ("uuid", "body_uuid"):
        v = body.get(k)
        if isinstance(v, str) and v:
            return v
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _query_page(api: str, logql: str, start_ns: int, end_ns: int, page_lines: int) -> list[tuple[int, dict, dict]]:
    """One query_range call over [start_ns, end_ns]. Returns a list of
    (ts_ns, stream_labels, body) triples ordered newest-first. Raises
    RuntimeError on transport/Loki errors; the ResourceExhausted cap surfaces
    as an HTTP 500 the caller recovers from by shrinking page_lines.
    """
    qs = urllib.parse.urlencode({
        "query":     logql,
        "start":     str(start_ns),
        "end":       str(end_ns),
        "limit":     str(page_lines),
        "direction": "backward",  # newest first; matches Speedscale cloud snapshot ordering
    })
    url = f"{api}?{qs}"

    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Loki returned HTTP {e.code}: {body[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach Loki at {api}: {e.reason}") from None

    if payload.get("status") != "success":
        raise RuntimeError(f"Loki returned non-success: {payload!r}")

    out: list[tuple[int, dict, dict]] = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # not our format — skip silently. The forwarder emits one
                # `body`-shaped object per line; anything else is noise (e.g.
                # health checks, debug exporter logs) we don't want.
                continue
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            out.append((int(ts_ns), labels, body))
    # query_range groups by stream, so values across streams aren't globally
    # sorted — sort newest-first so the cursor advances monotonically.
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def query_loki(loki_url: str, logql: str, start: datetime, end: datetime, limit: int) -> list[tuple[dict, dict]]:
    """Fetch up to `limit` matching RRPairs from Loki across [start,end].

    Returns a list of (stream_labels, body) pairs — one per matching log line.
    `body` is the RRPair JSON object originally emitted by the forwarder; the
    stream labels are the OTEL resource attrs that came in as Loki indexed
    labels (cluster, service, namespace, exporter).

    A single query_range can't return a large extract (Loki's ~4 MB response
    cap), so we page newest-first with a timestamp cursor and merge + dedup by
    RRPair uuid. Result order is newest-first, matching Speedscale cloud
    snapshot ordering, and is truncated to `limit` total records.
    """
    base = loki_url.rstrip("/")
    api = f"{base}/loki/api/v1/query_range"

    start_ns = int(start.timestamp() * 1_000_000_000)
    cursor_ns = int(end.timestamp() * 1_000_000_000)  # exclusive upper bound, walks down

    seen: set[str] = set()
    out: list[tuple[dict, dict]] = []

    while cursor_ns > start_ns and len(out) < limit:
        page_lines = _PAGE_LINES
        while True:
            try:
                page = _query_page(api, logql, start_ns, cursor_ns, page_lines)
                break
            except RuntimeError as e:
                # Fat RRPairs can still trip the cap at the default page size;
                # halve and retry. A single line over the cap can't be paged
                # around, so give up (re-raise) once we're down to one line.
                if _RESOURCE_EXHAUSTED not in str(e) or page_lines <= 1:
                    raise
                page_lines = max(1, page_lines // 2)

        if not page:
            break

        oldest_ns = page[-1][0]
        added_this_page = 0
        for ts_ns, labels, body in page:
            rid = _rrpair_id(body)
            if rid in seen:
                continue
            seen.add(rid)
            out.append((labels, body))
            added_this_page += 1
            if len(out) >= limit:
                return out

        # Advance the cursor just past the oldest line in this page. Loki's
        # `end` is exclusive, so setting it to oldest_ns re-queries that same
        # instant's lines (deduped above) and guarantees forward progress even
        # when many lines share a timestamp.
        next_cursor = oldest_ns
        if next_cursor >= cursor_ns:
            next_cursor = cursor_ns - 1
        cursor_ns = next_cursor

        # A full page that yielded only already-seen records means we're stuck
        # on a dense same-timestamp cluster larger than a page; step back 1ns to
        # keep moving rather than spin.
        if added_this_page == 0:
            cursor_ns -= 1

    return out


# ─── signature instance numbering ───────────────────────────────────────────


# When multiple recorded RRPairs share the same signature (same host+method+url+
# queryparams), proxymock dedupes them via an `instance: N` field added to the
# signature map. Cloud snapshots have this because the analyzer assigns it
# during snapshot processing — we skip the analyzer entirely (by design), so we
# have to assign instance numbers ourselves before writing.
#
# Without this, proxymock loads our files but the responder can't tell two
# /spacex/launches recordings apart and ends up rejecting requests with
# "your request did not match any mock signature" even when a match exists.


def _signature_key(sig: dict) -> tuple:
    """Stable comparison key for a signature map. Ignores any existing
    `instance` field so we re-number from scratch.
    """
    return tuple(sorted((k, v) for k, v in sig.items() if k != "instance"))


def assign_instances(records: list[tuple[dict, dict]]) -> None:
    """Mutate each record's `body.signature` to include an `instance` value.

    Records are processed in arrival order (which is `direction=backward` from
    Loki, i.e. newest first). For each unique signature, the first record gets
    instance="0", the second instance="1", and so on — matching the dedup
    contract proxymock's responder expects.
    """
    counts: dict[tuple, int] = {}
    for _stream, body in records:
        sig = body.get("signature")
        if not isinstance(sig, dict):
            continue
        key = _signature_key(sig)
        n = counts.get(key, 0)
        # Signature values are base64-encoded bytes when this object is the
        # proto JSON form; encode "N" the same way for consistency.
        sig["instance"] = base64.b64encode(str(n).encode()).decode()
        counts[key] = n + 1


# ─── RRPair fixups + file writing ───────────────────────────────────────────


# Some forwarder fields ship as the literal string "undefined" when not wired
# (see Linear S-11091 for the cluster case). The OTEL resource attribute is
# correct on the stream label, so we copy it down into the body before writing.
# Once S-11091 lands this becomes a no-op; the script stays correct either way.
_UNDEFINED_FIELDS = ("cluster", "namespace")


def fix_record(body: dict, stream: dict) -> dict:
    """Apply minimal fixups so the emitted RRPair matches what proxymock expects.

    Mutates `body` in place AND returns it (convenience).
    """
    for field in _UNDEFINED_FIELDS:
        if body.get(field) in ("undefined", "", None):
            label_val = stream.get(field)
            if label_val:
                body[field] = label_val
    return body


def base64_uuid_to_str(b64: str) -> str:
    """RRPair UUIDs ship as 16-byte values base64-encoded. proxymock writes them
    as RFC-4122 hyphenated strings in filenames; we do the same so the snapshot
    directory is byte-identical to a `proxymock record` output.
    """
    try:
        raw = base64.b64decode(b64, validate=False)
        if len(raw) == 16:
            return str(uuid_mod.UUID(bytes=raw))
    except (ValueError, TypeError):
        pass
    # Fall back to a deterministic-but-arbitrary uuid so a bad input doesn't
    # silently collapse multiple records onto the same filename.
    return str(uuid_mod.uuid4())


def write_rrpair(body: dict, snapshot_dir: Path) -> Path:
    """Write a single RRPair JSON file under <snapshot_dir>/<host>/<uuid>.json.

    The on-disk shape matches what `speedctl proxymock cloud pull snapshot`
    expands to, so `proxymock mock --in <out-dir>` reads it as-is via
    lib/rrfile/reader.go's DecodeRRFile path.
    """
    host = (body.get("http") or {}).get("req", {}).get("host") or "unknown-host"
    # Sanitize host for filesystem use — colons (e.g. host:port) and slashes
    # don't belong in directory names.
    host = re.sub(r"[^A-Za-z0-9._-]", "_", host)

    uuid_str = base64_uuid_to_str(body.get("uuid", ""))

    host_dir = snapshot_dir / host
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / f"{uuid_str}.json"
    path.write_text(json.dumps(body, separators=(",", ":")))
    return path


# ─── snapshot metadata ──────────────────────────────────────────────────────


def write_metadata(out_dir: Path, snapshot_id: str, logql: str, start: datetime, end: datetime, count: int) -> None:
    """Write `.metadata/snapshot.json` so downstream tooling can recognize this
    as a Speedscale-style snapshot directory.

    Field shape is intentionally a subset of the cloud Scenario proto — we mark
    `source: "loki"` and `analysisStatus: "none"` so anything that branches on
    cloud-style analysis (which doesn't apply here) can skip cleanly.
    """
    meta = {
        "id":             snapshot_id,
        "name":           f"loki-gather-{snapshot_id[:8]}",
        "source":         "loki",
        "analysisStatus": "none",
        "logql":          logql,
        "timeRange": {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end":   end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "rrpairCount":    count,
        "createdAt":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "createdBy":      "loki-gather.py",
    }
    meta_dir = out_dir / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "snapshot.json").write_text(json.dumps(meta, indent=2))


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="loki-gather.py",
        description="Pull a subset of BYOC RRPair traffic from Loki and write a proxymock-replayable directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:\n", 1)[1] if "Usage:" in (__doc__ or "") else None,
    )
    p.add_argument("--loki-url",  required=True, help="Base URL of the Loki HTTP API (e.g. http://localhost:38030)")
    p.add_argument("--out-dir",   required=True, help="Output directory for the proxymock snapshot tree")

    p.add_argument("--start",     default="-15m", help="Window start: 'now', '-15m', '-2h', '-1d', or RFC3339. Default: -15m")
    p.add_argument("--end",       default="now",  help="Window end: same formats as --start. Default: now")
    p.add_argument("--limit",     type=int, default=5000, help="Max total records to gather. The window is paged in time slices to stay under Loki's ~4 MB response cap, so this is the merged total, not a per-query cap. Default: 5000")

    p.add_argument("--cluster",   help="Filter by cluster (stream label)")
    p.add_argument("--service",   help="Filter by service (stream label)")
    p.add_argument("--namespace", help="Filter by namespace (stream label)")
    p.add_argument("--method",    help='Filter by HTTP method, regex (e.g. "GET", "POST|PUT")')
    p.add_argument("--status",    help='Filter by HTTP status, regex (e.g. "200", "2..", "[45]..")')
    p.add_argument("--endpoint",  help='Filter by URL path, regex (e.g. "^/api/.+")')
    p.add_argument("--direction", choices=("IN", "OUT"), help="Filter by traffic direction")

    p.add_argument("--logql", help="Full LogQL query — bypasses all the above filter flags. For power users.")

    p.add_argument("--dry-run", action="store_true", help="Print the resolved LogQL + window and exit without querying or writing")

    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        start = parse_time(args.start)
        end   = parse_time(args.end)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    logql = build_logql(args)

    print(f"loki-gather: {args.loki_url}", file=sys.stderr)
    print(f"  window: {start.isoformat()}  →  {end.isoformat()}  ({(end - start).total_seconds():.0f}s)", file=sys.stderr)
    print(f"  query:  {logql}", file=sys.stderr)

    if args.dry_run:
        print("dry run — exiting without querying Loki", file=sys.stderr)
        return 0

    try:
        records = query_loki(args.loki_url, logql, start, end, args.limit)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not records:
        print("no traffic matched filter; nothing written.", file=sys.stderr)
        print("hint: widen the time window, drop a filter, or use --dry-run to inspect the resolved query", file=sys.stderr)
        return 1

    # First pass: fixups (cluster=undefined, etc.) + assign instance numbers
    # for duplicate signatures so proxymock's responder can match deterministically.
    for stream, body in records:
        fix_record(body, stream)
    assign_instances(records)

    out_dir = Path(args.out_dir).expanduser().resolve()
    snapshot_id = str(uuid_mod.uuid4())
    snapshot_dir = out_dir / f"snapshot-{snapshot_id}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    hosts: dict[str, int] = {}
    for _stream, body in records:
        path = write_rrpair(body, snapshot_dir)
        host = path.parent.name
        hosts[host] = hosts.get(host, 0) + 1
        written += 1

    write_metadata(out_dir, snapshot_id, logql, start, end, written)

    print(f"wrote {written} RRPairs to {snapshot_dir}", file=sys.stderr)
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"  {host}: {n}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"replay with:  proxymock mock --in {out_dir} --listen :8080", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
