#!/usr/bin/env python3
"""Verify the Iceberg sink chart: hardening, env wiring, GOMEMLIMIT, and value guards."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = str(ROOT / "charts" / "iceberg")


def render(*args):
    output = subprocess.check_output(["helm", "template", "contract", CHART, *args], text=True)
    return [item for item in yaml.safe_load_all(output) if item]


def rejects(*args):
    result = subprocess.run(["helm", "template", "contract", CHART, *args], capture_output=True, text=True)
    assert result.returncode != 0, f"iceberg chart rendered with {args}"


def kind(objects, name):
    return [item for item in objects if item.get("kind") == name]


def env_of(container):
    return {item["name"]: item for item in container.get("env") or []}


def main():
    objects = render()
    deployment = kind(objects, "Deployment")[0]
    pod = deployment["spec"]["template"]
    container = pod["spec"]["containers"][0]
    assert pod["metadata"]["annotations"]["checksum/config"]
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "ALL" in container["securityContext"]["capabilities"]["drop"]
    assert container["image"] == "gcr.io/speedscale/iceberg-sink:latest"
    assert {p["containerPort"] for p in container["ports"]} == {4317, 8080}
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["lifecycle"]["preStop"]["exec"]["command"] == ["/bin/sleep", "5"]
    assert env_of(container)["GOMEMLIMIT"]["value"] == "409MiB"
    assert deployment["spec"]["replicas"] == 1

    service = kind(objects, "Service")[0]
    assert {p["port"] for p in service["spec"]["ports"]} == {4317, 8080}

    policy = kind(objects, "NetworkPolicy")[0]
    assert [rule["ports"][0]["port"] for rule in policy["spec"]["ingress"]] == [4317, 8080]

    config = kind(objects, "ConfigMap")[0]["data"]
    for key in (
        "ICEBERG_CATALOG_TYPE", "ICEBERG_CATALOG_URI", "ICEBERG_WAREHOUSE", "ICEBERG_NAMESPACE", "ICEBERG_TABLE",
        "ICEBERG_COMMIT_INTERVAL", "ICEBERG_MAX_BATCH_BYTES", "ICEBERG_MAX_PENDING_BYTES", "ICEBERG_COMMIT_RETRIES",
        "ICEBERG_MAINTENANCE", "AWS_REGION", "OTLP_GRPC_ADDR", "HTTP_ADDR",
    ):
        assert key in config, key
    assert "ICEBERG_CATALOG_PROPERTIES" not in config
    assert not kind(objects, "CronJob")

    inline = render("--set", "iceberg.catalog.properties.s3\\.endpoint=http://minio:9000")
    assert kind(inline, "ConfigMap")[0]["data"]["ICEBERG_CATALOG_PROPERTIES"] == '{"s3.endpoint":"http://minio:9000"}'

    secret = render(
        "--set", "iceberg.catalog.properties.s3\\.endpoint=http://minio:9000",
        "--set", "iceberg.catalog.propertiesSecret.name=catalog",
        "--set", "auth.mode=secret", "--set", "auth.secretName=storage",
        "--set", "auth.googleCredentialsKey=key.json",
        "--set", "azureWorkloadIdentity=true",
        "--set", "resources.limits.memory=1Gi",
    )
    pod = kind(secret, "Deployment")[0]["spec"]["template"]
    container = pod["spec"]["containers"][0]
    env = env_of(container)
    assert env["ICEBERG_CATALOG_PROPERTIES"]["valueFrom"]["secretKeyRef"] == {"name": "catalog", "key": "catalog-properties.json"}
    assert env["GOOGLE_APPLICATION_CREDENTIALS"]["value"] == "/var/run/iceberg/google/credentials.json"
    assert env["GOMEMLIMIT"]["value"] == "819MiB"
    assert {"secretRef": {"name": "storage"}} in container["envFrom"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert "ICEBERG_CATALOG_PROPERTIES" not in kind(secret, "ConfigMap")[0]["data"]

    s3tables = render(
        "--set", "iceberg.catalog.type=s3tables", "--set", "iceberg.catalog.uri=",
        "--set", "iceberg.catalog.warehouse=arn:aws:s3tables:us-east-1:111122223333:bucket/traffic",
    )
    assert kind(s3tables, "ConfigMap")[0]["data"]["ICEBERG_CATALOG_TYPE"] == "s3tables"

    compaction = render("--set", "compaction.enabled=true", "--set", "compaction.command={/opt/compact}")
    job = kind(compaction, "CronJob")[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = job["containers"][0]
    assert container["command"] == ["/opt/compact"]
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert env_of(container)["GOMEMLIMIT"]["value"] == "819MiB"
    assert {"configMapRef": {"name": "contract-iceberg"}} in container["envFrom"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True

    rejects("--set", "iceberg.catalog.uri=")
    rejects("--set", "iceberg.catalog.type=s3tables")
    rejects("--set", "iceberg.catalog.type=hive")
    rejects("--set", "iceberg.maintenance=sometimes")
    rejects("--set", "auth.mode=secret")
    rejects("--set", "auth.googleCredentialsKey=key.json")
    rejects("--set", "compaction.enabled=true")
    rejects("--set", "compaction.enabled=true", "--set", "compaction.command={/opt/compact}",
            "--set", "compaction.resources.limits.memory=512Mi")
    print("validated iceberg sink chart")


if __name__ == "__main__":
    main()
