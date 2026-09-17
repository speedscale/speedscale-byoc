#!/usr/bin/env python3
"""Render primary destination charts and enforce one backend per channel."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "fluentbit-s3": {"awss3", "debug"},
    "fluentbit-gcs": {"awss3", "debug"},
    "gcs": {"google_cloud_storage"},
    "azureblob": {"azure_blob", "debug"},
    "datadog": {"datadog"},
    "dynatrace": {"otlphttp/dynatrace"},
    "newrelic": {"otlphttp/newrelic"},
    "elasticsearch": {"elasticsearch", "debug"},
    "grafana": {"otlphttp/loki", "prometheusremotewrite", "debug"},
    "otlp": {"otlphttp/logs"},
    "splunk": {"splunk_hec"},
    "kafka": {"kafka"},
}


def collector_config(chart):
    rendered = subprocess.check_output(
        ["helm", "template", "boundary-test", str(ROOT / "charts" / chart)],
        text=True,
    )
    configs = []
    for item in yaml.safe_load_all(rendered):
        if not item or item.get("kind") != "ConfigMap":
            continue
        for key in ("otel.yaml", "collector.yaml"):
            if key in item.get("data", {}):
                configs.append(yaml.safe_load(item["data"][key]))
    if len(configs) != 1:
        raise AssertionError(f"{chart}: expected one collector config, found {len(configs)}")
    return configs[0]


def exporter_names(config):
    exporters = config.get("exporters")
    if not exporters:
        raise AssertionError("collector config has no exporters section")
    return set(exporters)


def main():
    for chart, expected in EXPECTED.items():
        actual = exporter_names(collector_config(chart))
        if actual != expected:
            raise AssertionError(f"{chart}: exporters {sorted(actual)} != {sorted(expected)}")

    datadog_recipe = (ROOT / "recipes/datadog-to-replay/gather.py").read_text().lower()
    for token in ("gcloud", "storage.googleapis.com", "--bucket", "gcs_objects"):
        if token in datadog_recipe:
            raise AssertionError(f"Datadog recipe still depends on GCS: {token}")

    print(f"validated {len(EXPECTED)} independent destination channels")


if __name__ == "__main__":
    main()
