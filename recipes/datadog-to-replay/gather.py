#!/usr/bin/env python3
"""Convert one partner Datadog trace into proxymock tests and mocks."""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.request
import uuid

SITES = {
    "datadoghq.com",
    "datadoghq.eu",
    "us3.datadoghq.com",
    "us5.datadoghq.com",
    "ap1.datadoghq.com",
    "ap2.datadoghq.com",
}


def request(url, headers, body):
    data = json.dumps(body).encode()
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=30
    ) as response:
        return response.read()


def search(site, headers, signal, query):
    results, cursor = [], None
    for _ in range(50):
        page = {"limit": 100}
        if cursor:
            page["cursor"] = cursor
        body = {
            "filter": {"from": "now-24h", "to": "now", "query": query},
            "sort": "timestamp",
            "page": page,
        }
        if signal == "spans":
            body = {"data": {"attributes": body, "type": "search_request"}}
        data = json.loads(
            request(f"https://api.{site}/api/v2/{signal}/events/search", headers, body)
        )
        results.extend(data.get("data", []))
        cursor = ((data.get("meta") or {}).get("page") or {}).get("after")
        if not cursor:
            return results
    raise ValueError("Query exceeded 5000 records; narrow the trace selection")


def unstringify(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if (stripped[:1], stripped[-1:]) not in (("[", "]"), ("{", "}")):
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        if stripped.startswith('["') and stripped.endswith('"]'):
            return [stripped[2:-2]]
        return value


def normalize(record):
    http = record.get("http")
    if isinstance(http, dict):
        for section in ("req", "res"):
            block = http.get(section)
            if isinstance(block, dict) and isinstance(block.get("headers"), dict):
                block["headers"] = {
                    key: unstringify(value)
                    for key, value in block["headers"].items()
                }
    token_list = record.get("tokenList")
    if isinstance(token_list, dict):
        for entry in token_list.values():
            if isinstance(entry, dict) and "tokens" in entry:
                entry["tokens"] = unstringify(entry["tokens"])
    return record


def event_record(event):
    attributes = (event.get("attributes") or {}).get("attributes")
    if not isinstance(attributes, dict):
        return None
    candidate = (
        attributes.get("body")
        if isinstance(attributes.get("body"), dict)
        else attributes
    )
    if candidate.get("msgType") != "rrpair":
        return None
    return normalize(candidate)


def record_filename(record):
    raw = record.get("uuid")
    try:
        return str(uuid.UUID(bytes=base64.b64decode(raw))) + ".json"
    except (TypeError, ValueError):
        if isinstance(raw, str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", raw):
            return str(uuid.UUID(raw)) + ".json"
        raise ValueError("RRPair has an invalid UUID") from None


def write_capture(out, trace_id, service, logs, spans):
    records = {}
    for event in logs:
        record = event_record(event)
        if not record:
            continue
        event_trace = str(
            (event.get("attributes") or {}).get("attributes", {}).get("otel.trace_id")
            or (event.get("attributes") or {}).get("attributes", {}).get("trace_id")
            or ""
        )
        if event_trace and event_trace != trace_id:
            raise ValueError("Datadog result contains a different trace")
        if record.get("l7protocol") == "http" and record.get("direction") in (
            "IN",
            "OUT",
        ):
            records[record_filename(record)] = record
    incoming = {
        name: item for name, item in records.items() if item["direction"] == "IN"
    }
    outgoing = {
        name: item for name, item in records.items() if item["direction"] == "OUT"
    }
    if not incoming or not outgoing:
        raise ValueError(
            "The trace must contain incoming test traffic and outgoing HTTP dependency captures"
        )
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    for folder, items in (("tests", incoming), ("mocks", outgoing)):
        directory = out / folder
        directory.mkdir(mode=0o700)
        for name, item in items.items():
            target = directory / name
            target.write_text(json.dumps(item, indent=2))
            target.chmod(0o600)
    provenance = {
        "trace_id": trace_id,
        "service": service,
        "source": "datadog",
        "log_ids": [event["id"] for event in logs if event_record(event)],
        "apm_spans": [
            {
                "resource": item.get("attributes", {}).get("resource_name"),
                "span_id": item.get("attributes", {}).get("span_id"),
            }
            for item in spans
        ],
        "tests": len(incoming),
        "mocks": len(outgoing),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2))
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--service", default="api-gateway")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch("[0-9a-f]{32}", args.trace_id):
        parser.error("Expected a 32-character lowercase trace ID")
    if not re.fullmatch("[A-Za-z0-9._-]+", args.service):
        parser.error("Invalid service name")
    settings = {
        key: os.environ.get("DATADOG_PARTNER_" + key, "")
        for key in ("API_KEY", "APP_KEY", "SITE")
    }
    if not all(settings.values()) or settings["SITE"] not in SITES:
        parser.error(
            "Explicit DATADOG_PARTNER_API_KEY, DATADOG_PARTNER_APP_KEY "
            "and valid DATADOG_PARTNER_SITE are required"
        )
    headers = {
        "Content-Type": "application/json",
        "DD-API-KEY": settings["API_KEY"],
        "DD-APPLICATION-KEY": settings["APP_KEY"],
    }
    base = f"env:partner-demo service:{args.service}"
    logs = search(
        settings["SITE"],
        headers,
        "logs",
        base + f" @otel.trace_id:{args.trace_id} @msgType:rrpair",
    )
    spans = search(
        settings["SITE"], headers, "spans", base + f" trace_id:{args.trace_id}"
    )
    if not logs or not spans:
        raise SystemExit(
            "Both same-service Datadog RRPair logs and APM spans are required"
        )
    try:
        provenance = write_capture(args.out, args.trace_id, args.service, logs, spans)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(
        json.dumps(
            {
                "trace_id": args.trace_id,
                "tests": provenance["tests"],
                "mocks": provenance["mocks"],
                "out": str(args.out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
