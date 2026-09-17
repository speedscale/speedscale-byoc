#!/usr/bin/env python3
"""Verify rollout, ingress, and metadata-only security contracts."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARDENED = ["azureblob", "datadog", "dynatrace", "elasticsearch", "fluentbit-gcs", "fluentbit-s3", "gcs", "grafana", "newrelic", "otlp", "splunk", "kafka"]


def render(chart, *args):
    output = subprocess.check_output(["helm", "template", "contract", str(ROOT / "charts" / chart), *args], text=True)
    return [item for item in yaml.safe_load_all(output) if item]


def main():
    for chart in HARDENED:
        objects = render(chart)
        deployment = next(
            item for item in objects
            if item.get("kind") == "Deployment"
            and any("opentelemetry-collector" in container["image"] for container in item["spec"]["template"]["spec"]["containers"])
        )
        pod = deployment["spec"]["template"]
        container = pod["spec"]["containers"][0]
        assert pod["metadata"]["annotations"]["checksum/config"]
        assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert "ALL" in container["securityContext"]["capabilities"]["drop"]
        assert any(item.get("kind") == "NetworkPolicy" for item in objects)

    for chart in ("otlp", "splunk", "kafka"):
        args = ["--set", "securityProfile.enabled=true"] if chart == "otlp" else []
        objects = render(chart, *args)
        configmap = next(item for item in objects if item.get("kind") == "ConfigMap")
        config = "\n".join(configmap.get("data", {}).values())
        assert 'event.kind' in config
        assert 'network.direction' in config
        assert 'set(body, attributes)' in config or 'set(log.body, log.attributes)' in config

    full = render("otlp", "--set", "securityProfile.enabled=true", "--set", "securityProfile.includeFullPayload=true")
    config = "\n".join(next(item for item in full if item.get("kind") == "ConfigMap").get("data", {}).values())
    assert 'set(log.body, log.attributes)' not in config
    print("validated rollout, pod hardening, network policy, and security profiles")


if __name__ == "__main__":
    main()
