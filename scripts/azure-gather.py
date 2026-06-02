#!/usr/bin/env python3
"""azure-gather.py — pull a subset of BYOC RRPair traffic from an Azure Blob
Storage container and assemble a proxymock-replayable directory.

Sibling of gcs-gather.py / s3-gather.py / loki-gather.py. Same CLI shape, same
output shape. Only the source differs: Azure Blob instead of GCS/S3.

Object layout (azure_blob exporter):
    <container>/YYYY/MM/DD/logs_HH_MM_SS.json_NNNN
    Each object is uncompressed OTLP-JSON (marshaler: otlp_json) — a single
    JSON document with a resourceLogs/scopeLogs/logRecords tree. RRPair fields
    live in the logRecord body as a kvlistValue, exactly like the GCS/S3 awss3
    exporter writes.

Note the layout differs from the GCS/S3 Hive style
(byoc/year=.../hour=.../minute=...): Azure partitions by *day* only, with the
HH_MM_SS encoded in the blob name. So we prune by day prefix(es) covering the
window, then filter individual blobs by the HH_MM_SS in the name down to the
[start, end] window.

Usage:

    python3 azure-gather.py \\
      --container          byoc \\
      --connection-string  "$AZURE_CONNECTION_STRING" \\
      --service            payment \\
      --status             2.. \\
      --endpoint           '^/api/.+' \\
      --start              -15m \\
      --out-dir            /tmp/snapshot

    proxymock mock --in /tmp/snapshot

    # Dry run — show which day prefixes the window touches + matching blobs
    python3 azure-gather.py --container byoc --start -15m --dry-run

Auth: shells out to the Azure CLI (`az storage blob ...`) with an Azure Storage
connection string. Read from --connection-string, or the AZURE_CONNECTION_STRING
env var (e.g. `source byoc-azureblob.env`). No Python SDK dependency — stdlib +
subprocess only, matching the style of the other gather scripts.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid as uuid_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Concurrency for blob downloads. Each worker shells out to `az storage blob
# download`, so this is I/O-bound — a healthy pool hides per-blob CLI startup.
DOWNLOAD_WORKERS = 16

# Blob names look like logs_HH_MM_SS.json_NNNN — pull the HH:MM:SS out so we can
# filter individual blobs to the window, since the day prefix is coarse.
_BLOB_TIME_RE = re.compile(r"logs_(\d{2})_(\d{2})_(\d{2})\.json")


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
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return now - delta
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise ValueError(
            f"can't parse time {s!r} — use 'now', '-15m'/'-2h'/'-1d', or RFC3339"
        )


# ─── partition (day prefix) enumeration ─────────────────────────────────────


def day_prefixes(start: datetime, end: datetime) -> list[str]:
    """Return YYYY/MM/DD/ prefixes covering every day the window touches.

    The azure_blob exporter partitions only by day, so a window that straddles
    midnight UTC yields two prefixes. e.g. 2026-06-01T23:55 → 2026-06-02T00:10:
      ['2026/06/01/', '2026/06/02/']
    """
    start_d = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_d = end.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[str] = []
    cur = start_d
    while cur <= end_d:
        out.append(f"{cur.year:04d}/{cur.month:02d}/{cur.day:02d}/")
        cur += timedelta(days=1)
    return out


def blob_in_window(name: str, day_prefix: str,
                   start: datetime, end: datetime) -> bool:
    """True if the HH_MM_SS encoded in the blob name falls in [start, end].

    The day component comes from the prefix the blob was listed under; the
    time-of-day comes from the logs_HH_MM_SS in the name. Blobs whose name
    doesn't carry a parseable timestamp are kept (we can't prune them safely).
    """
    m = _BLOB_TIME_RE.search(name)
    if not m:
        return True
    try:
        y, mo, d = (int(x) for x in day_prefix.strip("/").split("/"))
    except (ValueError, AttributeError):
        return True
    hh, mm, ss = (int(g) for g in m.groups())
    ts = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
    return start <= ts <= end


# ─── Azure Blob helpers (shell out to `az` — uses the connection string) ─────


def _az_base() -> list[str]:
    """Resolve the `az` binary. Honors PATH; falls back to the Homebrew path."""
    for cand in ("az", "/opt/homebrew/bin/az"):
        if cand == "az" or Path(cand).exists():
            return [cand]
    return ["az"]


def list_blobs(container: str, conn: str, prefix: str) -> list[str]:
    """`az storage blob list --prefix <prefix>` — return blob names under prefix.

    Returns [] if the prefix doesn't exist or the container is empty there.
    """
    cmd = (_az_base() + [
        "storage", "blob", "list",
        "--container-name", container,
        "--connection-string", conn,
        "--prefix", prefix,
        "--query", "[].name",
        "-o", "tsv",
    ])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError("az CLI not found on PATH — install Azure CLI (brew install azure-cli)")
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "ContainerNotFound" in stderr or "BlobNotFound" in stderr:
            return []
        raise RuntimeError(f"az storage blob list failed: {stderr[:300]}")
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().endswith(".json") or ".json_" in line.strip()
    ]


def download_blob(container: str, conn: str, name: str) -> bytes:
    """Download a blob's content to a temp file and return its raw bytes.

    `az storage blob download --file -` does NOT stream the blob body to
    stdout — it prints a JSON result object describing the download. So we
    must download to a real file and read it back.
    """
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="az-gather-")
    os.close(fd)
    try:
        cmd = (_az_base() + [
            "storage", "blob", "download",
            "--container-name", container,
            "--connection-string", conn,
            "--name", name,
            "--file", tmp,
            "--no-progress",
            "-o", "none",
        ])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"az storage blob download failed for {name}: "
                f"{proc.stderr.strip()[:300]}"
            )
        return Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ─── OTLP-JSON deserialization (azure_blob exporter format) ─────────────────
# Identical to gcs-gather.py / s3-gather.py — the azure_blob exporter writes
# the same OTLP-JSON (marshaler: otlp_json) blobs as the GCS/S3 awss3 exporter.


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
    """Convert an OTLP kvlistValue values array to a Python dict."""
    result = {}
    for kv in values:
        result[kv["key"]] = _otlp_value(kv.get("value", {}))
    return result


def parse_otlp_json(data: bytes) -> list[dict]:
    """Parse an OTLP-JSON object (marshaler: otlp_json) and return a list of
    flat RRPair body dicts — one per logRecord. The cluster attribute from the
    resourceLog's resource is backfilled onto each record when the body lacks it.
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


# ─── record filtering ───────────────────────────────────────────────────────


def regex_match(pattern: str | None, value) -> bool:
    if pattern is None:
        return True
    if value is None:
        return False
    return re.search(pattern, str(value)) is not None


def record_matches(rec: dict, args: argparse.Namespace) -> bool:
    """Filter a flat RRPair record against the CLI filter flags."""
    internal_attrs = (
        rec.get("__internal__", {})
           .get("log_metadata", {})
           .get("otlp", {})
           .get("attributes", {})
    )

    if args.service and rec.get("service") not in (args.service, None):
        if internal_attrs.get("service") != args.service:
            return False
    if args.namespace and rec.get("namespace") not in (args.namespace, None):
        if internal_attrs.get("namespace") != args.namespace:
            return False

    if not regex_match(args.method, rec.get("command")):
        return False
    if not regex_match(args.status, rec.get("status")):
        return False
    if not regex_match(args.endpoint, rec.get("location")):
        return False
    if args.direction and rec.get("direction") != args.direction:
        return False

    return True


# ─── RRPair fixups ──────────────────────────────────────────────────────────


def fix_record(rec: dict) -> dict:
    """Strip OTLP envelope metadata, leaving the canonical RRPair body that
    proxymock expects. Backfill cluster/namespace from the OTLP envelope when
    the body shipped them as 'undefined'/empty (same forwarder workaround the
    other gather scripts apply).
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


# ─── signature instance numbering ───────────────────────────────────────────


def _signature_key(sig: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in sig.items() if k != "instance"))


def assign_instances(records: list[dict]) -> None:
    """Number same-signature records identically to gcs/s3/loki/es-gather so a
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


# ─── filename derivation + writing ──────────────────────────────────────────


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
    """Write <snapshot_dir>/<host>/<uuid>.json — same layout as gcs/s3-gather."""
    host = (rec.get("http") or {}).get("req", {}).get("host") or "unknown-host"
    host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    uuid_str = base64_uuid_to_str(rec.get("uuid", ""))
    host_dir = snapshot_dir / host
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / f"{uuid_str}.json"
    path.write_text(json.dumps(rec, separators=(",", ":")))
    return path


# ─── snapshot metadata ──────────────────────────────────────────────────────


def write_metadata(out_dir: Path, snapshot_id: str, container: str,
                   start: datetime, end: datetime,
                   prefixes: list[str], object_count: int,
                   rrpair_count: int) -> None:
    """Write `.metadata/snapshot.json`. `source: azure` so downstream tooling
    can distinguish from a gcs/s3/loki/es-gather snapshot.
    """
    meta = {
        "id":             snapshot_id,
        "name":           f"azure-gather-{snapshot_id[:8]}",
        "source":         "azure",
        "analysisStatus": "none",
        "azureContainer": container,
        "azurePrefixes":  prefixes,
        "objectCount":    object_count,
        "rrpairCount":    rrpair_count,
        "timeRange": {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end":   end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "createdAt":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "createdBy":      "azure-gather.py",
    }
    meta_dir = out_dir / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "snapshot.json").write_text(json.dumps(meta, indent=2))


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="azure-gather.py",
        description="Pull a subset of BYOC RRPair traffic from an Azure Blob "
                    "Storage container and write a proxymock-replayable directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:\n", 1)[1] if "Usage:" in (__doc__ or "") else None,
    )
    p.add_argument("--container", default=os.environ.get("AZURE_CONTAINER", "byoc"),
                   help="Azure Blob container name (env AZURE_CONTAINER, default: byoc)")
    p.add_argument("--connection-string", default=os.environ.get("AZURE_CONNECTION_STRING"),
                   help="Azure Storage connection string (env AZURE_CONNECTION_STRING)")
    p.add_argument("--out-dir", required=True,
                   help="Output directory for the proxymock snapshot tree")

    p.add_argument("--start", default="-15m",
                   help="Window start: 'now', '-15m', '-2h', '-1d', or RFC3339. Default: -15m")
    p.add_argument("--end", default="now",
                   help="Window end: same formats as --start. Default: now")

    p.add_argument("--service",   help="Filter by service name (exact match)")
    p.add_argument("--namespace", help="Filter by k8s namespace (exact match)")
    p.add_argument("--method",    help='Filter by HTTP method, regex (e.g. "GET", "POST|PUT")')
    p.add_argument("--status",    help='Filter by HTTP status, regex (e.g. "200", "2..", "[45]..")')
    p.add_argument("--endpoint",  help='Filter by URL path, regex (e.g. "^/api/.+")')
    p.add_argument("--direction", choices=("IN", "OUT"), help="Filter by traffic direction")

    p.add_argument("--dry-run", action="store_true",
                   help="List prefixes + matching blobs without downloading or writing")

    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.connection_string:
        print("error: no connection string — pass --connection-string or set "
              "AZURE_CONNECTION_STRING (e.g. `source byoc-azureblob.env`)",
              file=sys.stderr)
        return 2

    try:
        start = parse_time(args.start)
        end = parse_time(args.end)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    conn = args.connection_string
    prefixes = day_prefixes(start, end)

    print(f"azure-gather: container={args.container}", file=sys.stderr)
    print(f"  window:     {start.isoformat()}  →  {end.isoformat()}  "
          f"({(end - start).total_seconds():.0f}s)", file=sys.stderr)
    print(f"  prefixes:   {len(prefixes)} day prefix(es) checked", file=sys.stderr)

    # List blobs under each day prefix concurrently, then filter by the
    # HH_MM_SS in the blob name down to the [start, end] window.
    all_blobs: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            listings = list(pool.map(
                lambda pfx: (pfx, list_blobs(args.container, conn, pfx)), prefixes))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for pfx, names in listings:
        for name in names:
            if blob_in_window(name, pfx, start, end):
                all_blobs.append(name)
    print(f"  blobs:      {len(all_blobs)}", file=sys.stderr)

    if args.dry_run:
        for b in all_blobs:
            print(f"    {b}", file=sys.stderr)
        print("dry run — exiting without downloading", file=sys.stderr)
        return 0

    if not all_blobs:
        print("no blobs found in window; nothing written.", file=sys.stderr)
        print("hint: widen --start, or check that the container is receiving traffic", file=sys.stderr)
        return 1

    # Download every blob concurrently, then parse OTLP-JSON records.
    def _fetch(name: str):
        try:
            return name, download_blob(args.container, conn, name)
        except RuntimeError as e:
            return name, e

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        results = list(pool.map(_fetch, all_blobs))

    matched: list[dict] = []
    for name, raw in results:
        if isinstance(raw, Exception):
            print(f"warning: {raw}", file=sys.stderr)
            continue
        for rec in parse_otlp_json(raw):
            if record_matches(rec, args):
                matched.append(rec)

    if not matched:
        print("no traffic matched filters; nothing written.", file=sys.stderr)
        print("hint: drop a filter or widen --start to inspect what's in the container", file=sys.stderr)
        return 1

    # Apply RRPair fixups + signature numbering
    for rec in matched:
        fix_record(rec)
    assign_instances(matched)

    # Write snapshot tree
    out_dir = Path(args.out_dir).expanduser().resolve()
    snapshot_id = str(uuid_mod.uuid4())
    snapshot_dir = out_dir / f"snapshot-{snapshot_id}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    hosts: dict[str, int] = {}
    protos: dict[str, int] = {}
    written = 0
    for rec in matched:
        proto = rec.get("l7protocol") or "unknown"
        protos[proto] = protos.get(proto, 0) + 1
        path = write_rrpair(rec, snapshot_dir)
        host = path.parent.name
        hosts[host] = hosts.get(host, 0) + 1
        written += 1

    write_metadata(out_dir, snapshot_id, args.container, start, end,
                   prefixes, len(all_blobs), written)

    print(f"wrote {written} RRPairs to {snapshot_dir}", file=sys.stderr)
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"  {host}: {n}", file=sys.stderr)
    print("  l7protocol breakdown:", file=sys.stderr)
    for proto, n in sorted(protos.items(), key=lambda kv: -kv[1]):
        print(f"    {proto}: {n}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"replay with:  proxymock mock --in {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
