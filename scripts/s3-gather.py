#!/usr/bin/env python3
"""
s3-gather.py — Pull a time window of RRPairs from an S3 data-lake bucket and
write a proxymock-replayable snapshot directory.

Sibling of gcs-gather.py / loki-gather.py. Same CLI shape, same output shape.

Two object layouts are supported:

Legacy (Fluent Bit s3 output, chart < 2.0.0):
    s3://<bucket>/year=YYYY/month=MM/day=DD/hour=HH/<uuid>-<idx>.json.gz
    Each object is gzipped NDJSON, one flat RRPair dict per line.

Current (OTel Collector awss3 exporter, chart >= 2.0.0):
    s3://<bucket>/byoc/year=YYYY/month=MM/day=DD/hour=HH/minute=MM/logs_<id>.json
    Each object is uncompressed OTLP-JSON (marshaler: otlp_json) — a single
    JSON document with a resourceLogs/scopeLogs/logRecords tree. RRPair fields
    live in the logRecord body as a kvlistValue.

Requirements:
  - Python 3.9+ (stdlib only — no pip install)
  - AWS CLI v2 authenticated (aws configure, env vars, SSO, or EC2/EKS IAM)

Usage:
  python3 s3-gather.py \\
    --bucket   speedscale-rrpair-demo-s3 \\
    --region   us-east-1 \\
    --service  payment \\
    --status   2.. \\
    --endpoint '^/api/.+' \\
    --start    -15m \\
    --out-dir  /tmp/snapshot

  # Dry run — show which S3 prefixes the window touches
  python3 s3-gather.py --bucket speedscale-rrpair-demo-s3 --start -15m --dry-run
"""

import argparse
import base64
import gzip
import json
import re
import subprocess
import sys
import uuid as uuid_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Concurrency for object downloads. Each worker shells out to `aws s3 cp`, so
# this is I/O-bound — a healthy pool hides per-object CLI startup latency.
DOWNLOAD_WORKERS = 16


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_relative(spec: str) -> datetime:
    """Parse relative specs like -15m, -2h, -1d into a UTC datetime."""
    spec = spec.strip()
    if spec.startswith("-"):
        spec = spec[1:]
    unit = spec[-1]
    val = int(spec[:-1])
    delta = {"s": timedelta(seconds=val), "m": timedelta(minutes=val),
             "h": timedelta(hours=val), "d": timedelta(days=val)}.get(unit)
    if delta is None:
        raise ValueError(f"Unknown time unit '{unit}' in spec '{spec}'")
    return datetime.now(timezone.utc) - delta


def parse_time(spec: str) -> datetime:
    if spec.startswith("-"):
        return parse_relative(spec)
    return datetime.fromisoformat(spec.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Partition enumeration
# ---------------------------------------------------------------------------

def hour_partitions(start: datetime, end: datetime, prefix: str = "") -> list[str]:
    """Hive-style prefixes covering every hour the window touches.

    Used for the legacy Fluent Bit layout, which has no minute= sub-partition.
    """
    cur = start.replace(minute=0, second=0, microsecond=0)
    end_h = end.replace(minute=0, second=0, microsecond=0)
    out: list[str] = []
    while cur <= end_h:
        out.append(
            f"{prefix}year={cur.year:04d}/month={cur.month:02d}/"
            f"day={cur.day:02d}/hour={cur.hour:02d}/"
        )
        cur += timedelta(hours=1)
    return out


def minute_partitions(start: datetime, end: datetime, prefix: str = "") -> list[str]:
    """Hive-style prefixes covering every *minute* the window touches.

    The awss3 exporter writes objects under
      <prefix>year=YYYY/month=MM/day=DD/hour=HH/minute=MM/logs_*.json
    so pruning to just the minutes in [start, end] avoids listing/downloading
    the whole ~1300-object hour partition when the caller only wants a few
    minutes of traffic.
    """
    cur = start.replace(second=0, microsecond=0)
    end_m = end.replace(second=0, microsecond=0)
    out: list[str] = []
    while cur <= end_m:
        out.append(
            f"{prefix}year={cur.year:04d}/month={cur.month:02d}/"
            f"day={cur.day:02d}/hour={cur.hour:02d}/minute={cur.minute:02d}/"
        )
        cur += timedelta(minutes=1)
    return out


# ---------------------------------------------------------------------------
# S3 helpers (shell out to aws CLI — inherits creds/SSO/IRSA automatically)
# ---------------------------------------------------------------------------

def _aws_base(args: argparse.Namespace) -> list[str]:
    cmd = ["aws", "s3"]
    return cmd


def _aws_global_flags(args: argparse.Namespace) -> list[str]:
    flags = ["--region", args.region]
    if args.profile:
        flags += ["--profile", args.profile]
    return flags


def s3_ls(bucket: str, prefix: str, args: argparse.Namespace) -> list[str]:
    """`aws s3 ls --recursive` — return s3://bucket/key strings under prefix.

    Recursive so we descend past minute= sub-prefixes to the actual .json
    objects (a non-recursive ls at the hour level only lists minute=NN/ dirs,
    which is the bug this replaces). Matches .json and .json.gz only.
    """
    cmd = (_aws_base(args)
           + ["ls", f"s3://{bucket}/{prefix}", "--recursive"]
           + _aws_global_flags(args))
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return []
    keys: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        # `--recursive` lines look like: DATE TIME SIZE  full/key/path
        if len(parts) < 4:
            continue
        key = parts[3]
        if key.endswith(".json") or key.endswith(".json.gz"):
            keys.append(f"s3://{bucket}/{key}")
    return keys


def s3_cat(s3_uri: str, args: argparse.Namespace) -> bytes:
    cmd = (_aws_base(args)
           + ["cp", s3_uri, "-"]
           + _aws_global_flags(args))
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# OTLP-JSON deserialization (awss3 exporter format) — mirrors gcs-gather.py
# ---------------------------------------------------------------------------

def _otlp_value(v: dict):
    """Unwrap a single OTLP AnyValue dict to a Python scalar / dict / list."""
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "bytesValue" in v:
        return v["bytesValue"]  # base64 string
    if "kvlistValue" in v:
        return _kvlist_to_dict(v["kvlistValue"].get("values", []))
    if "arrayValue" in v:
        return [_otlp_value(av) for av in v["arrayValue"].get("values", [])]
    return None


def _kvlist_to_dict(values: list) -> dict:
    result = {}
    for kv in values:
        result[kv["key"]] = _otlp_value(kv.get("value", {}))
    return result


def parse_otlp_json(data: bytes) -> list[dict]:
    """Parse an OTLP-JSON object and return a list of flat RRPair body dicts —
    one per logRecord. The cluster attribute from the resourceLog's resource
    is backfilled onto each record when the body lacks it.
    """
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return []

    records: list[dict] = []
    for rl in obj.get("resourceLogs", []):
        res_attrs = _kvlist_to_dict(rl.get("resource", {}).get("attributes", []))
        for sl in rl.get("scopeLogs", []):
            for lr in sl.get("logRecords", []):
                body = lr.get("body", {})
                if "kvlistValue" not in body:
                    continue
                rec = _kvlist_to_dict(body["kvlistValue"].get("values", []))
                if not rec.get("cluster") and res_attrs.get("cluster"):
                    rec["cluster"] = res_attrs["cluster"]
                records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def regex_match(pattern, value) -> bool:
    if pattern is None:
        return True
    if value is None:
        return False
    return re.search(pattern, str(value)) is not None


def matches(record: dict, args: argparse.Namespace) -> bool:
    if args.service and record.get("service", "") != args.service:
        return False
    if args.namespace and record.get("namespace", "") != args.namespace:
        return False
    if args.method:
        if not regex_match(args.method, record.get("command")):
            return False
    if args.status:
        if not regex_match(args.status, record.get("status")):
            return False
    if args.endpoint:
        if not regex_match(args.endpoint, record.get("location")):
            return False
    if args.direction:
        if record.get("direction", "") != args.direction:
            return False
    return True


# ---------------------------------------------------------------------------
# RRPair fixups + filename derivation (mirrors gcs-gather.py)
# ---------------------------------------------------------------------------

def fix_record(rec: dict) -> dict:
    """Strip Fluent Bit / OTLP envelope metadata, leaving the canonical RRPair
    body proxymock expects. Backfill cluster/namespace from the OTLP envelope
    when the body shipped them as 'undefined'/empty.
    """
    internal = rec.pop("__internal__", {}) or {}
    rec.pop("@timestamp", None)

    res_attrs = (internal.get("group_attributes", {})
                         .get("resource", {})
                         .get("attributes", {})) or {}
    otlp_attrs = (internal.get("log_metadata", {})
                          .get("otlp", {})
                          .get("attributes", {})) or {}

    if rec.get("cluster") in ("undefined", "", None) and res_attrs.get("cluster"):
        rec["cluster"] = res_attrs["cluster"]
    if rec.get("namespace") in ("undefined", "", None) and otlp_attrs.get("namespace"):
        rec["namespace"] = otlp_attrs["namespace"]
    return rec


def _signature_key(sig: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in sig.items() if k != "instance"))


def assign_instances(records: list[dict]) -> None:
    """Number same-signature records identically to gcs/loki/es-gather so a
    snapshot written by any of them dedupes the same way downstream.
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


def base64_uuid_to_str(b64: str) -> str:
    """RRPair UUIDs ship as 16-byte base64; convert to hyphenated RFC-4122."""
    try:
        raw = base64.b64decode(b64, validate=False)
        if len(raw) == 16:
            return str(uuid_mod.UUID(bytes=raw))
    except (ValueError, TypeError):
        pass
    return str(uuid_mod.uuid4())


def write_rrpair(rec: dict, snapshot_dir: Path) -> Path:
    """Write <snapshot_dir>/<host>/<uuid>.json — same layout as gcs-gather."""
    host = (rec.get("http") or {}).get("req", {}).get("host") or "unknown-host"
    host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    uuid_str = base64_uuid_to_str(rec.get("uuid", ""))
    host_dir = snapshot_dir / host
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / f"{uuid_str}.json"
    path.write_text(json.dumps(rec, separators=(",", ":")))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Pull RRPairs from S3 → proxymock snapshot")
    ap.add_argument("--bucket",    required=True, help="S3 bucket name")
    ap.add_argument("--region",    default="us-east-1", help="AWS region")
    ap.add_argument("--profile",   default=None, help="AWS named profile (optional)")
    ap.add_argument("--start",     default="-1h", help="Start time (-15m, -2h, ISO8601)")
    ap.add_argument("--end",       default="now", help="End time (default: now)")
    ap.add_argument("--service",   help="Filter by service name (exact)")
    ap.add_argument("--namespace", help="Filter by namespace (exact)")
    ap.add_argument("--method",    help="Filter by HTTP method (regex)")
    ap.add_argument("--status",    help="Filter by HTTP status (regex, e.g. 2..)")
    ap.add_argument("--endpoint",  help="Filter by URL path (regex)")
    ap.add_argument("--direction", choices=["IN", "OUT"], help="Capture direction")
    ap.add_argument("--limit",     type=int, default=5000, help="Max records to gather")
    ap.add_argument("--out-dir",   default="/tmp/s3-snapshot", help="Output directory")
    ap.add_argument("--dry-run",   action="store_true", help="Show prefixes without downloading")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    try:
        start_dt = parse_time(args.start)
        end_dt = datetime.now(timezone.utc) if args.end == "now" else parse_time(args.end)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Enumerate both legacy (no prefix, hour granularity) and current
    # (byoc/ prefix, minute granularity) partitions. Minute-level pruning on
    # the byoc layout avoids scanning the whole ~1300-object hour partition.
    legacy_partitions = hour_partitions(start_dt, end_dt, prefix="")
    byoc_partitions = minute_partitions(start_dt, end_dt, prefix="byoc/")
    all_partitions = legacy_partitions + byoc_partitions

    print(f"s3-gather: s3://{args.bucket}", file=sys.stderr)
    print(f"  window:     {start_dt.isoformat()} → {end_dt.isoformat()} "
          f"({(end_dt - start_dt).total_seconds():.0f}s)", file=sys.stderr)
    print(f"  partitions: {len(all_partitions)} prefix(es) checked", file=sys.stderr)

    if args.dry_run:
        for prefix in all_partitions:
            print(f"  s3://{args.bucket}/{prefix}")
        return 0

    # List objects across all partitions concurrently.
    all_objs: list[str] = []
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for objs in pool.map(lambda p: s3_ls(args.bucket, p, args), all_partitions):
            all_objs.extend(objs)
    print(f"  objects:    {len(all_objs)}", file=sys.stderr)

    if not all_objs:
        print("no objects found in window; nothing written.", file=sys.stderr)
        print("hint: widen --start, or check that the bucket is receiving traffic", file=sys.stderr)
        return 1

    # Download every object concurrently, then parse records.
    def _fetch(s3_uri: str):
        try:
            return s3_uri, s3_cat(s3_uri, args)
        except Exception as e:  # noqa: BLE001 — surface as warning, keep going
            return s3_uri, e

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        results = list(pool.map(_fetch, all_objs))

    matched: list[dict] = []
    for s3_uri, raw in results:
        if isinstance(raw, Exception):
            print(f"  WARN: failed to read {s3_uri}: {raw}", file=sys.stderr)
            continue

        if s3_uri.endswith(".json.gz"):
            # Legacy: gzipped NDJSON, one flat RRPair dict per line
            try:
                data = gzip.decompress(raw)
            except OSError:
                data = raw
            for line in data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and matches(rec, args):
                    matched.append(rec)
        else:
            # Current: OTLP-JSON document (marshaler: otlp_json)
            for rec in parse_otlp_json(raw):
                if matches(rec, args):
                    matched.append(rec)
        if len(matched) >= args.limit:
            break

    if len(matched) > args.limit:
        matched = matched[:args.limit]

    if not matched:
        print("no traffic matched filters; nothing written.", file=sys.stderr)
        print("hint: drop a filter or widen --start to inspect what's in the bucket", file=sys.stderr)
        return 1

    for rec in matched:
        fix_record(rec)
    assign_instances(matched)

    # Write snapshot tree
    out_root = Path(args.out_dir).expanduser().resolve()
    snap_id = str(uuid_mod.uuid4())
    snap_dir = out_root / f"snapshot-{snap_id}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = out_root / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)

    hosts: dict[str, int] = {}
    written = 0
    for rec in matched:
        path = write_rrpair(rec, snap_dir)
        host = path.parent.name
        hosts[host] = hosts.get(host, 0) + 1
        written += 1

    (meta_dir / "snapshot.json").write_text(json.dumps({
        "id":          snap_id,
        "name":        f"s3-gather-{snap_id[:8]}",
        "source":      "s3",
        "bucket":      args.bucket,
        "region":      args.region,
        "partitions":  all_partitions,
        "objectCount": len(all_objs),
        "rrpairCount": written,
        "timeRange": {
            "start": start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end":   end_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "createdAt":   datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "createdBy":   "s3-gather.py",
    }, indent=2))

    print(f"wrote {written} RRPairs to {snap_dir}", file=sys.stderr)
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"  {host}: {n}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"replay with:  proxymock mock --in {out_root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
