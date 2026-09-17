#!/usr/bin/env python3
"""Render primary destination charts and enforce one backend per channel."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "fluentbit-s3": {"awss3", "debug"},
    "gcs": {"google_cloud_storage"},
    "datadog": {"datadog"},
    "dynatrace": {"otlphttp/dynatrace"},
    "newrelic": {"otlphttp/newrelic"},
}


def collector_config(chart):
    rendered = subprocess.check_output(
        ["helm", "template", "boundary-test", str(ROOT / "charts" / chart)],
        text=True,
    )
    matches = re.findall(r"  otel\.yaml: \|\n(.*?)(?=\n---|\Z)", rendered, re.DOTALL)
    if len(matches) != 1:
        raise AssertionError(f"{chart}: expected one collector config, found {len(matches)}")
    return "\n".join(line[4:] if line.startswith("    ") else line for line in matches[0].splitlines())


def exporter_names(config):
    match = re.search(r"^exporters:\n(.*?)(?=^[a-z_]+:\n)", config, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError("collector config has no exporters section")
    return {
        item.group(1)
        for item in re.finditer(r"^  ([A-Za-z0-9_./-]+):", match.group(1), re.MULTILINE)
    }


def main():
    for chart, expected in EXPECTED.items():
        actual = exporter_names(collector_config(chart))
        if actual != expected:
            raise AssertionError(f"{chart}: exporters {sorted(actual)} != {sorted(expected)}")

    datadog_recipe = (ROOT / "recipes/datadog-to-replay/gather.py").read_text().lower()
    for token in ("gcloud", "storage.googleapis.com", "--bucket", "gcs_objects"):
        if token in datadog_recipe:
            raise AssertionError(f"Datadog recipe still depends on GCS: {token}")

    print("validated independent S3, GCS, Datadog, Dynatrace, and New Relic channels")


if __name__ == "__main__":
    main()
