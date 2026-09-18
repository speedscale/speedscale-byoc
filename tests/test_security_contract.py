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

    newrelic = render("newrelic")
    config = "\n".join(next(item for item in newrelic if item.get("kind") == "ConfigMap").get("data", {}).values())
    for required in (
        'log.attributes["hostname"]',
        'log.attributes["server.address"]',
        'log.attributes["speedscale.protocol"]',
        'set(log.body, log.cache["summary"])',
        "groupbyattrs/service:",
        "span_metrics/newrelic:",
        "new_name: http.server.duration",
        "metrics/newrelic_apm:",
    ):
        assert required in config
    assert "processors: [memory_limiter, transform/captures, groupbyattrs/service, batch]" in config
    assert "exporters: [otlphttp/newrelic, span_metrics/newrelic]" in config

    disabled = subprocess.run(
        [
            "helm", "template", "contract", str(ROOT / "charts" / "otlp"),
            "--set", "otlp.signals.logs=false",
            "--set", "otlp.signals.traces=false",
            "--set", "otlp.signals.metrics=false",
        ],
        capture_output=True,
        text=True,
    )
    assert disabled.returncode != 0, "OTLP chart rendered with all signals disabled"

    example = ROOT / "examples" / "security" / "opensearch"
    compose = yaml.safe_load((example / "docker-compose.yaml").read_text())
    assert set(compose["services"]) == {"opensearch", "dashboards", "data-prepper", "collector"}
    assert compose["services"]["opensearch"]["ports"] == ["127.0.0.1:9200:9200"]
    pipeline = yaml.safe_load((example / "pipelines.yaml").read_text())
    source = pipeline["speedscale-security"]["source"]["otlp"]
    assert source["logs_path"] == "/v1/logs"
    assert source["logs_output_format"] == "opensearch"
    assert pipeline["speedscale-security"]["sink"][0]["opensearch"]["index"] == "speedscale-security-%{yyyy-MM-dd}"
    processors = pipeline["speedscale-security"]["processor"]
    assert any(item.get("parse_json", {}).get("source") == "body" for item in processors)
    assert any(
        {"from_key": "http@response@status_code", "to_key": "http/response/status_code"} in item.get("rename_keys", {}).get("entries", [])
        for item in processors
    )
    collector = (example / "collector.yaml").read_text()
    assert 'set(log.body, log.attributes)' in collector
    assert "http.response.status_code" in collector
    rule = yaml.safe_load((example / "rules" / "authentication-failure.yml").read_text())
    assert rule["detection"]["condition"] == "authentication_endpoint and rejected"
    print("validated rollout, pod hardening, network policy, and security profiles")


if __name__ == "__main__":
    main()
