#!/usr/bin/env python3
"""Validate every collector config with its chart's pinned image."""

import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "azureblob": {"env": ["AZURE_BLOB_URL=https://validation.blob.core.windows.net", "AZURE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=validation;AccountKey=dmFsaWRhdGlvbg==;EndpointSuffix=core.windows.net"]},
    "datadog": {"env": ["DD_API_KEY=" + "0" * 32]},
    "dynatrace": {"env": ["DT_API_TOKEN=dt0c01.validation"]},
    "elasticsearch": {},
    "fluentbit-gcs": {"env": ["AWS_ACCESS_KEY_ID=validation", "AWS_SECRET_ACCESS_KEY=validation"]},
    "fluentbit-s3": {"env": ["AWS_ACCESS_KEY_ID=validation", "AWS_SECRET_ACCESS_KEY=validation"]},
    "gcs": {},
    "grafana": {},
    "kafka": {},
    "newrelic": {"env": ["NEW_RELIC_LICENSE_KEY=validation"]},
    "otlp": {"env": ["OTLP_TOKEN=validation", "OTLP_LOGS_ENDPOINT=https://example.com/v1/logs", "OTLP_BASE_ENDPOINT=https://example.com"]},
    "otlp-traces": {"chart": "otlp", "helm": ["--set", "otlp.signals.logs=false", "--set", "otlp.signals.traces=true", "--set", "otlp.baseEndpoint=https://example.com"], "env": ["OTLP_TOKEN=validation", "OTLP_LOGS_ENDPOINT=https://example.com/v1/logs", "OTLP_BASE_ENDPOINT=https://example.com"]},
    "otlp-metrics": {"chart": "otlp", "helm": ["--set", "otlp.signals.logs=false", "--set", "otlp.signals.metrics=true", "--set", "otlp.baseEndpoint=https://example.com"], "env": ["OTLP_TOKEN=validation", "OTLP_LOGS_ENDPOINT=https://example.com/v1/logs", "OTLP_BASE_ENDPOINT=https://example.com"]},
    "otlp-all-signals": {"chart": "otlp", "helm": ["--set", "otlp.signals.traces=true", "--set", "otlp.signals.metrics=true", "--set", "otlp.baseEndpoint=https://example.com"], "env": ["OTLP_TOKEN=validation", "OTLP_LOGS_ENDPOINT=https://example.com/v1/logs", "OTLP_BASE_ENDPOINT=https://example.com"]},
    "otlp-security": {"chart": "otlp", "helm": ["--set", "securityProfile.enabled=true"], "env": ["OTLP_TOKEN=validation", "OTLP_LOGS_ENDPOINT=https://example.com/v1/logs", "OTLP_BASE_ENDPOINT=https://example.com"]},
    "splunk": {"env": ["SPLUNK_HEC_TOKEN=validation"]},
}


def main():
    for name, case in CASES.items():
        chart = case.get("chart", name)
        rendered = subprocess.check_output(
            ["helm", "template", "collector-validation", str(ROOT / "charts" / chart), *case.get("helm", [])],
            text=True,
        )
        objects = list(yaml.safe_load_all(rendered))
        configmap = next(
            item for item in objects
            if item and item.get("kind") == "ConfigMap" and ({"otel.yaml", "collector.yaml"} & set(item.get("data", {})))
        )
        config_key = next(key for key in ("otel.yaml", "collector.yaml") if key in configmap["data"])
        config = configmap["data"][config_key]
        deployments = [item for item in objects if item and item.get("kind") == "Deployment"]
        image = next(
            container["image"]
            for deployment in deployments
            for container in deployment["spec"]["template"]["spec"]["containers"]
            if "opentelemetry-collector" in container["image"]
        )
        environment = [flag for value in case.get("env", []) for flag in ("-e", value)]
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder, "otel.yaml")
            config_path.write_text(config)
            config_path.chmod(0o644)
            Path(folder).chmod(0o755)
            subprocess.run(
                ["docker", "run", "--rm", "--network=none", *environment, "-v", f"{folder}:/conf:ro", image, "validate", "--config=/conf/otel.yaml"],
                check=True,
            )
        print(f"validated {name} collector with {image}")

    rendered = subprocess.check_output(
        ["helm", "template", "loki-validation", str(ROOT / "charts" / "grafana")],
        text=True,
    )
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    config = next(item for item in objects if item.get("kind") == "ConfigMap" and "loki.yaml" in item.get("data", {}))["data"]["loki.yaml"]
    image = next(
        container["image"]
        for item in objects
        if item.get("kind") == "Deployment"
        for container in item["spec"]["template"]["spec"]["containers"]
        if container["name"] == "loki"
    )
    with tempfile.TemporaryDirectory() as folder:
        config_path = Path(folder, "loki.yaml")
        config_path.write_text(config)
        config_path.chmod(0o644)
        Path(folder).chmod(0o755)
        subprocess.run(
            ["docker", "run", "--rm", "--network=none", "-v", f"{folder}:/conf:ro", image, "-config.file=/conf/loki.yaml", "-verify-config=true"],
            check=True,
        )
    print(f"validated grafana Loki config with {image}")


if __name__ == "__main__":
    main()
