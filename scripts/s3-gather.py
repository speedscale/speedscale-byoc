#!/usr/bin/env python3
"""
s3-gather.py — Pull a time window of RRPairs from an S3 bucket (byoc-fluentbit-s3
scenario) and write a proxymock-replayable snapshot directory.

Sibling of gcs-gather.py / loki-gather.py. Same CLI shape, same output shape.

Requirements:
  - Python 3.9+ (stdlib only — no pip install)
  - AWS CLI v2 authenticated (aws configure, env vars, or EC2/EKS IAM)
  - The S3 bucket written by the fluentbit-s3 chart

Usage:
  python3 s3-gather.py \\
    --bucket   my-rrpair-archive \\
    --region   us-east-1 \\
    --service  java-server \\
    --status   2.. \\
    --endpoint '^/api/.+' \\
    --start    -1h \\
    --out-dir  /tmp/snapshot

  # Dry run — show which S3 prefixes the window touches
  python3 s3-gather.py --bucket my-rrpair-archive --start -1h --dry-run
"""

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path


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
    return datetime.fromisoformat(spec).astimezone(timezone.utc)


def hour_range(start: datetime, end: datetime):
    """Yield (year, month, day, hour) tuples covering [start, end]."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        yield cur.year, cur.month, cur.day, cur.hour
        cur += timedelta(hours=1)


# ---------------------------------------------------------------------------
# S3 helpers (shell out to aws CLI — inherits creds/IRSA automatically)
# ---------------------------------------------------------------------------

def s3_ls(bucket: str, prefix: str, region: str) -> list[str]:
    """Return s3://bucket/key strings under prefix."""
    cmd = ["aws", "s3", "ls", f"s3://{bucket}/{prefix}", "--region", region]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return []
    keys = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            keys.append(f"s3://{bucket}/{prefix}{parts[3]}")
        elif len(parts) == 1:
            # Directory listing — subdirectory
            pass
    return keys


def s3_cat(s3_uri: str, region: str) -> bytes:
    cmd = ["aws", "s3", "cp", s3_uri, "-", "--region", region]
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def matches(record: dict, args: argparse.Namespace) -> bool:
    if args.service and record.get("service", "") != args.service:
        return False
    if args.namespace and record.get("namespace", "") != args.namespace:
        return False
    if args.method:
        if not re.fullmatch(args.method, record.get("command", ""), re.IGNORECASE):
            return False
    if args.status:
        if not re.fullmatch(args.status, str(record.get("status", ""))):
            return False
    if args.endpoint:
        http = record.get("http", {})
        url = http.get("req", {}).get("url", "") if isinstance(http, dict) else ""
        if not re.search(args.endpoint, url):
            return False
    if args.direction:
        if record.get("direction", "") != args.direction:
            return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pull RRPairs from S3 → proxymock snapshot")
    ap.add_argument("--bucket",    required=True, help="S3 bucket name")
    ap.add_argument("--region",    default="us-east-1", help="AWS region")
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
    args = ap.parse_args()

    start_dt = parse_time(args.start)
    end_dt = datetime.now(timezone.utc) if args.end == "now" else parse_time(args.end)

    print(f"Window: {start_dt.isoformat()} → {end_dt.isoformat()}", file=sys.stderr)

    # Enumerate partitions
    partitions = list(hour_range(start_dt, end_dt))
    print(f"Partitions to scan: {len(partitions)}", file=sys.stderr)

    if args.dry_run:
        for y, mo, d, h in partitions:
            prefix = f"year={y}/month={mo:02d}/day={d:02d}/hour={h:02d}/"
            print(f"  s3://{args.bucket}/{prefix}")
        return

    # Gather records
    snap_id = str(uuid.uuid4())
    out_root = Path(args.out_dir)
    snap_dir = out_root / f"snapshot-{snap_id}"
    meta_dir = out_root / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)

    gathered = 0
    objects_scanned = 0
    hosts_seen: set[str] = set()

    for y, mo, d, h in partitions:
        if gathered >= args.limit:
            break
        prefix = f"year={y}/month={mo:02d}/day={d:02d}/hour={h:02d}/"
        objects = s3_ls(args.bucket, prefix, args.region)
        for obj_uri in objects:
            if gathered >= args.limit:
                break
            objects_scanned += 1
            try:
                raw = s3_cat(obj_uri, args.region)
                if obj_uri.endswith(".gz"):
                    raw = gzip.decompress(raw)
            except Exception as e:
                print(f"  WARN: failed to read {obj_uri}: {e}", file=sys.stderr)
                continue

            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Backfill cluster/namespace from OTLP envelope if present
                internal = record.pop("__internal__", {})
                if not record.get("cluster"):
                    resource = internal.get("log_metadata", {}).get("otlp", {}).get("resource", {})
                    record["cluster"] = resource.get("cluster", "")
                if not record.get("namespace"):
                    attrs = internal.get("group_attributes", {})
                    record["namespace"] = attrs.get("namespace", "")

                if not matches(record, args):
                    continue

                host = record.get("service", "unknown")
                hosts_seen.add(host)
                host_dir = snap_dir / host
                host_dir.mkdir(parents=True, exist_ok=True)

                rrpair_id = record.get("id") or str(uuid.uuid4())
                record.setdefault("instance", gathered)
                (host_dir / f"{rrpair_id}.json").write_text(json.dumps(record, indent=2))
                gathered += 1
                if gathered >= args.limit:
                    break

    print(f"Objects scanned: {objects_scanned}", file=sys.stderr)
    print(f"Records gathered: {gathered}", file=sys.stderr)

    if gathered == 0:
        print("No matching records found.", file=sys.stderr)
        sys.exit(1)

    # Write snapshot metadata
    (meta_dir / "snapshot.json").write_text(json.dumps({
        "id": snap_id,
        "source": "s3",
        "bucket": args.bucket,
        "region": args.region,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "partitions": [
            f"year={y}/month={mo:02d}/day={d:02d}/hour={h:02d}/"
            for y, mo, d, h in partitions
        ],
        "records": gathered,
        "hosts": sorted(hosts_seen),
    }, indent=2))

    print(f"Snapshot written to: {out_root}", file=sys.stderr)
    print(f"Run: proxymock mock --in {out_root}", file=sys.stderr)


if __name__ == "__main__":
    main()
