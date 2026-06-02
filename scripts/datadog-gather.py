#!/usr/bin/env python3
"""datadog-gather.py — pull a subset of BYOC RRPair traffic back out of Datadog
Logs and write a proxymock-replayable directory.

This is the Datadog sibling of `es-gather.py` / `loki-gather.py` / `gcs-gather.py`.
The forwarder ships each RRPair to Datadog as a log event whose body is the
RRPair itself. In the Logs Search API each result event looks like:

    data[].attributes = {
        service, status, timestamp, tags,
        attributes: { ...the RRPair body... }   # msgType=rrpair, http/kafka/...
    }

That nested `attributes` dict IS the canonical RRPair body — the same dict the
other gather scripts write to disk — so we reconstruct it as-is and reuse the
identical snapshot writer. The one Datadog-specific fixup (`fix_record`) reverses
Datadog's habit of coercing structured fields — HTTP header values and DLP token
lists — into JSON-encoded *strings*; left unreversed, proxymock fails to
unmarshal them.

Usage:

    python3 datadog-gather.py \\
      --service  payment \\
      --status   2.. \\
      --endpoint '^/balance' \\
      --start    -30m \\
      --out-dir  /tmp/dd-snapshot

    proxymock mock --in /tmp/dd-snapshot

Auth: reads DATADOG_API_KEY / DATADOG_APP_KEY from the environment (a same-org
key + application key pair). `source` your byoc-datadog.env first, or pass
--api-key / --app-key. The Datadog site (and thus API host) defaults to the
DATADOG_SITE env var, then datadoghq.com.

stdlib + urllib only, matching the house style of es-gather.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Datadog Logs Search caps page.limit at 1000.
MAX_PAGE_LIMIT = 1000


# ─── time parsing ───────────────────────────────────────────────────────────


def parse_time(s: str, *, now: datetime | None = None) -> datetime:
    """Accept 'now', a relative offset like '-15m' / '-1h' / '-2d', or RFC3339."""
    now = now or datetime.now(timezone.utc)
    s = s.strip()
    if s in ("now", ""):
        return now
    m = re.fullmatch(r"-(\d+)([smhd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return now - delta
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"can't parse time {s!r} — use 'now', '-15m'/'-2h'/'-1d', or RFC3339")


def to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ─── Datadog query construction ─────────────────────────────────────────────


def build_query(args: argparse.Namespace) -> str:
    """Translate CLI filter flags into a Datadog Logs search query string.

    Every RRPair log carries the facets we filter on (`@msgType`, `@service`,
    `@direction`, `@l7protocol`, `@command`, `@status`, `@location`). Server-side
    filtering keeps the result set small so pagination stays cheap.
    """
    terms = ["@msgType:rrpair"]
    if args.service:
        terms.append(f"@service:{args.service}")
    if args.direction:
        terms.append(f"@direction:{args.direction}")
    if args.l7protocol:
        terms.append(f"@l7protocol:{args.l7protocol}")
    if args.method:
        terms.append(f"@command:{args.method}")
    if args.status:
        terms.append(f"@status:{args.status}")
    if args.endpoint:
        terms.append(f"@location:{args.endpoint}")
    return " ".join(terms)


# ─── Datadog Logs Search API ────────────────────────────────────────────────


def search_logs(api_host: str, api_key: str, app_key: str, query: str,
                start: datetime, end: datetime, limit: int) -> list[dict]:
    """Page through POST /api/v2/logs/events/search until `limit` events are
    collected or the result set is exhausted.

    Returns a list of RRPair body dicts — the nested `attributes.attributes`
    of each event, which is the RRPair as the forwarder emitted it.
    """
    url = f"https://{api_host}/api/v2/logs/events/search"
    headers = {
        "Content-Type": "application/json",
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }

    bodies: list[dict] = []
    cursor: str | None = None

    while len(bodies) < limit:
        page_limit = min(MAX_PAGE_LIMIT, limit - len(bodies))
        page: dict = {"limit": page_limit}
        if cursor:
            page["cursor"] = cursor

        payload = json.dumps({
            "filter": {
                "query": query,
                "from": to_rfc3339(start),
                "to": to_rfc3339(end),
            },
            "sort": "-timestamp",   # newest first; mirrors the other gathers
            "page": page,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Datadog returned HTTP {e.code}: {err_body[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"can't reach Datadog at {url}: {e.reason}") from None

        if resp.get("errors"):
            raise RuntimeError(f"Datadog returned errors: {resp['errors']!r}")

        data = resp.get("data", [])
        for ev in data:
            body = (ev.get("attributes") or {}).get("attributes")
            if isinstance(body, dict):
                bodies.append(body)

        cursor = (resp.get("meta") or {}).get("page", {}).get("after")
        if not cursor or not data:
            break

    return bodies[:limit]


# ─── Datadog fixups ─────────────────────────────────────────────────────────


def _unstringify_json(v):
    """If `v` is a string that holds a JSON array or object, return the parsed
    value; otherwise return `v` unchanged.

    Datadog's log pipeline coerces structured RRPair fields into JSON-encoded
    *strings* (a facet artifact). The OTLP/ES/GCS sources preserve native
    arrays, so proxymock's proto-JSON reader expects native arrays here too —
    e.g. header values must be `["v1","v2"]`, not the literal string
    `'["v1","v2"]'`. Without this reversal proxymock fails to unmarshal the
    RRPair ("unexpected token \"[...]\"").
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if (s[:1], s[-1:]) not in (("[", "]"), ("{", "}")):
        return v
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return v


def _unstringify_header(v):
    """Header-value flavour of `_unstringify_json` with a lenient fallback.

    Datadog wraps each header value in a JSON array string, e.g.
    `'["Go-http-client/1.1"]'`. Most parse cleanly, but values containing
    raw double-quotes (notably Envoy/Istio `X-Forwarded-Client-Cert`, whose
    `Subject=""` is left unescaped) are emitted as *invalid* JSON. For those
    we salvage the value by stripping the outer `["` … `"]` wrapper so the
    header still round-trips into a single-element array rather than being
    dropped or breaking the proto unmarshal.
    """
    if not isinstance(v, str):
        return v
    parsed = _unstringify_json(v)
    if parsed is not v:
        return parsed
    s = v.strip()
    if s.startswith('["') and s.endswith('"]'):
        return [s[2:-2]]
    return v


def fix_record(rec: dict) -> dict:
    """Reverse Datadog's stringification so the RRPair matches the native shape
    proxymock reads (identical to what gcs-gather / es-gather emit).

    Two fields are affected in practice:
      - HTTP header values  (http.req/res.headers.<Name>) → JSON array
      - DLP token lists      (tokenList.<id>.tokens)        → JSON array
    """
    http = rec.get("http")
    if isinstance(http, dict):
        for section in ("req", "res"):
            blk = http.get(section)
            if isinstance(blk, dict) and isinstance(blk.get("headers"), dict):
                blk["headers"] = {
                    k: _unstringify_header(v) for k, v in blk["headers"].items()
                }

    token_list = rec.get("tokenList")
    if isinstance(token_list, dict):
        for entry in token_list.values():
            if isinstance(entry, dict) and "tokens" in entry:
                entry["tokens"] = _unstringify_json(entry["tokens"])

    return rec


# ─── signature instance numbering ───────────────────────────────────────────


def _signature_key(sig: dict) -> tuple:
    """Stable comparison key for a signature map, ignoring any existing
    `instance` field so we re-number from scratch."""
    return tuple(sorted((k, v) for k, v in sig.items() if k != "instance"))


def assign_instances(records: list[dict]) -> None:
    """Mutate each record's `signature` to include an `instance` value.

    Identical to the other gather scripts — proxymock's responder dedupes
    same-signature records via this field, so a snapshot written by any of
    them behaves the same downstream.
    """
    counts: dict[tuple, int] = {}
    for rec in records:
        sig = rec.get("signature")
        if not isinstance(sig, dict):
            continue
        key = _signature_key(sig)
        n = counts.get(key, 0)
        sig["instance"] = base64.b64encode(str(n).encode()).decode()
        counts[key] = n + 1


# ─── filename derivation + writing ──────────────────────────────────────────


def base64_uuid_to_str(b64: str) -> str:
    """RRPair UUIDs ship as 16-byte values base64-encoded; convert to
    hyphenated RFC-4122 form for the filename."""
    try:
        raw = base64.b64decode(b64, validate=False)
        if len(raw) == 16:
            return str(uuid_mod.UUID(bytes=raw))
    except (ValueError, TypeError):
        pass
    return str(uuid_mod.uuid4())


def write_rrpair(rec: dict, snapshot_dir: Path) -> Path:
    """Write <snapshot_dir>/<host>/<uuid>.json — same layout as the other gathers."""
    host = (rec.get("http") or {}).get("req", {}).get("host") or "unknown-host"
    host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    uuid_str = base64_uuid_to_str(rec.get("uuid", ""))
    host_dir = snapshot_dir / host
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / f"{uuid_str}.json"
    path.write_text(json.dumps(rec, separators=(",", ":")))
    return path


# ─── snapshot metadata ──────────────────────────────────────────────────────


def write_metadata(out_dir: Path, snapshot_id: str, query: str, api_host: str,
                   start: datetime, end: datetime, count: int) -> None:
    """Write `.metadata/snapshot.json`. `source: datadog` so downstream tooling
    can distinguish from a loki-gather / es-gather / gcs-gather snapshot."""
    meta = {
        "id":             snapshot_id,
        "name":           f"datadog-gather-{snapshot_id[:8]}",
        "source":         "datadog",
        "analysisStatus": "none",
        "datadogHost":    api_host,
        "datadogQuery":   query,
        "timeRange": {
            "start": to_rfc3339(start),
            "end":   to_rfc3339(end),
        },
        "rrpairCount":    count,
        "createdAt":      to_rfc3339(datetime.now(timezone.utc)),
        "createdBy":      "datadog-gather.py",
    }
    meta_dir = out_dir / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "snapshot.json").write_text(json.dumps(meta, indent=2))


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="datadog-gather.py",
        description="Pull a subset of BYOC RRPair traffic from Datadog Logs and write a proxymock-replayable directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:\n", 1)[1] if "Usage:" in (__doc__ or "") else None,
    )
    p.add_argument("--out-dir",   required=True, help="Output directory for the proxymock snapshot tree")

    p.add_argument("--site",      default=os.environ.get("DATADOG_SITE", "datadoghq.com"),
                   help="Datadog site (e.g. datadoghq.com, datadoghq.eu, us5.datadoghq.com). "
                        "API host is api.<site>. Default: $DATADOG_SITE or datadoghq.com")
    p.add_argument("--api-key",   default=os.environ.get("DATADOG_API_KEY"),
                   help="Datadog API key. Default: $DATADOG_API_KEY")
    p.add_argument("--app-key",   default=os.environ.get("DATADOG_APP_KEY"),
                   help="Datadog application key. Default: $DATADOG_APP_KEY")

    p.add_argument("--start",     default="-15m", help="Window start: 'now', '-15m', '-2h', '-1d', or RFC3339. Default: -15m")
    p.add_argument("--end",       default="now",  help="Window end: same formats as --start. Default: now")
    p.add_argument("--limit",     type=int, default=300, help="Max RRPairs to gather (paginated 1000/page). Default: 300")

    p.add_argument("--service",    help="Filter by @service (exact match)")
    p.add_argument("--direction",  choices=("IN", "OUT"), help="Filter by @direction")
    p.add_argument("--l7protocol", help="Filter by @l7protocol (e.g. http, kafka, postgres, mysql)")
    p.add_argument("--method",     help='Filter by @command (HTTP method), e.g. "GET", "(POST OR PUT)"')
    p.add_argument("--status",     help='Filter by @status, e.g. "200", "2..", "(4?? OR 5??)"')
    p.add_argument("--endpoint",   help='Filter by @location (URL path), e.g. "/balance", "\\/api\\/*"')

    p.add_argument("--dry-run", action="store_true", help="Print the resolved query + window and exit without querying or writing")

    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.dry_run and (not args.api_key or not args.app_key):
        print("error: missing Datadog credentials — `source` your byoc-datadog.env "
              "(DATADOG_API_KEY + DATADOG_APP_KEY) or pass --api-key/--app-key", file=sys.stderr)
        return 2

    try:
        start = parse_time(args.start)
        end   = parse_time(args.end)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    api_host = f"api.{args.site.lstrip('.')}"
    query = build_query(args)

    print(f"datadog-gather: {api_host}", file=sys.stderr)
    print(f"  window: {start.isoformat()}  →  {end.isoformat()}  ({(end - start).total_seconds():.0f}s)", file=sys.stderr)
    print(f"  query:  {query}", file=sys.stderr)
    print(f"  limit:  {args.limit}", file=sys.stderr)

    if args.dry_run:
        print("dry run — exiting without querying Datadog", file=sys.stderr)
        return 0

    try:
        records = search_logs(api_host, args.api_key, args.app_key, query, start, end, args.limit)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not records:
        print("no traffic matched filter; nothing written.", file=sys.stderr)
        print("hint: widen the time window, drop a filter, or use --dry-run to inspect the resolved query", file=sys.stderr)
        return 1

    for rec in records:
        fix_record(rec)
    assign_instances(records)

    out_dir = Path(args.out_dir).expanduser().resolve()
    snapshot_id = str(uuid_mod.uuid4())
    snapshot_dir = out_dir / f"snapshot-{snapshot_id}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    hosts: dict[str, int] = {}
    protocols: dict[str, int] = {}
    for rec in records:
        path = write_rrpair(rec, snapshot_dir)
        host = path.parent.name
        hosts[host] = hosts.get(host, 0) + 1
        proto = rec.get("l7protocol") or "unknown"
        protocols[proto] = protocols.get(proto, 0) + 1
        written += 1

    write_metadata(out_dir, snapshot_id, query, api_host, start, end, written)

    print(f"wrote {written} RRPairs to {snapshot_dir}", file=sys.stderr)
    print("  by host:", file=sys.stderr)
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"    {host}: {n}", file=sys.stderr)
    print("  by l7protocol:", file=sys.stderr)
    for proto, n in sorted(protocols.items(), key=lambda kv: -kv[1]):
        print(f"    {proto}: {n}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"replay with:  proxymock mock --in {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
