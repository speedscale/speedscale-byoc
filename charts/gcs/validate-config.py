#!/usr/bin/env python3
"""Validate the GCS collector and reader contract."""

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

chart = Path(__file__).resolve().parent


def validate_collector_config(config, image):
    with tempfile.TemporaryDirectory() as folder:
        Path(folder).chmod(0o755)
        Path(folder, "otel.yaml").write_text(config)
        Path(folder, "otel.yaml").chmod(0o644)
        subprocess.run(
            ["docker", "run", "--rm", "--network=none", "-v", f"{folder}:/conf:ro", image, "validate", "--config=/conf/otel.yaml"],
            check=True,
        )


rendered = subprocess.check_output(["helm", "template", "test", str(chart)], text=True)
objects = list(yaml.safe_load_all(rendered))
config = next(item for item in objects if item["kind"] == "ConfigMap")["data"]["otel.yaml"]
parsed = yaml.safe_load(config)
assert set(parsed["exporters"]) == {"google_cloud_storage"}
assert set(parsed["service"]["pipelines"]) == {"logs"}
assert parsed["service"]["pipelines"]["logs"]["exporters"] == ["google_cloud_storage"]
assert "storage" not in parsed["exporters"]["google_cloud_storage"]["sending_queue"]
assert "file_storage" not in parsed["extensions"]
assert not any(item["kind"] == "PersistentVolumeClaim" for item in objects)
deployment = next(item for item in objects if item["kind"] == "Deployment")
assert "strategy" not in deployment["spec"]
assert all(volume["name"] != "queue" for volume in deployment["spec"]["template"]["spec"]["volumes"])
assert all(mount["name"] != "queue" for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"])
assert "env" not in deployment["spec"]["template"]["spec"]["containers"][0]
assert all(volume["name"] != "google-credentials" for volume in deployment["spec"]["template"]["spec"]["volumes"])
image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
validate_collector_config(config, image)
print(json.dumps({"channel": "gcs", "exporters": ["google_cloud_storage"], "status": "valid"}))

persistent_args = ["helm", "template", "test", str(chart), "--set", "delivery.persistence.enabled=true"]
persistent_objects = list(yaml.safe_load_all(subprocess.check_output(persistent_args, text=True)))
persistent_config = next(item for item in persistent_objects if item["kind"] == "ConfigMap")["data"]["otel.yaml"]
persistent_parsed = yaml.safe_load(persistent_config)
assert persistent_parsed["exporters"]["google_cloud_storage"]["sending_queue"]["storage"] == "file_storage"
assert "file_storage" in persistent_parsed["extensions"]
assert any(item["kind"] == "PersistentVolumeClaim" for item in persistent_objects)
persistent_deployment = next(item for item in persistent_objects if item["kind"] == "Deployment")
assert persistent_deployment["spec"]["strategy"]["type"] == "Recreate"
assert any(volume["name"] == "queue" for volume in persistent_deployment["spec"]["template"]["spec"]["volumes"])
assert any(mount["name"] == "queue" for mount in persistent_deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"])
validate_collector_config(persistent_config, image)
print(json.dumps({"channel": "gcs-persistent-queue", "status": "valid"}))

reader_args = ["helm", "template", "test", str(chart), "--set", "reader.enabled=true", "--set", "reader.image=example/reader:validation", "--set", "reader.rbac.create=true", "--set", "reader.rbac.subjects[0].kind=Group", "--set", "reader.rbac.subjects[0].name=traffic-readers"]
objects = list(yaml.safe_load_all(subprocess.check_output(reader_args, text=True)))
reader = next(item for item in objects if item["kind"] == "StatefulSet")
collector = next(item for item in objects if item["kind"] == "Deployment")
assert all(item["name"] != "GOOGLE_APPLICATION_CREDENTIALS" for item in reader["spec"]["template"]["spec"]["containers"][0]["env"])
assert reader["spec"]["template"]["spec"]["serviceAccountName"] != collector["spec"]["template"]["spec"]["serviceAccountName"]
config = next(item for item in objects if item["kind"] == "ConfigMap" and "reader.json" in item.get("data", {}))
bindings = json.loads(config["data"]["reader.json"])
assert bindings["version"] == 1
assert bindings["destinations"][0]["prefix"].endswith("/")
collector_config = next(item for item in objects if item["kind"] == "ConfigMap" and "otel.yaml" in item.get("data", {}))
assert collector_config["metadata"]["annotations"]["byoc.speedscale.com/reader-pod"] == reader["metadata"]["name"] + "-0"
role = next(item for item in objects if item["kind"] == "Role")
resources = {resource for rule in role["rules"] for resource in rule["resources"]}
assert resources == {"configmaps", "pods", "pods/portforward"}
for rule in role["rules"]:
    if "pods/portforward" in rule["resources"]:
        assert rule["resourceNames"] == [reader["metadata"]["name"] + "-0"]
        assert rule["verbs"] == ["create"]
for overrides in (["reader.image="], ["reader.serviceAccount.name=same", "serviceAccount.name=same"], ["reader.rbac.subjects=[]"]):
    command = list(reader_args)
    for override in overrides:
        command += ["--set", override]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0, f"Invalid reader settings were accepted: {overrides}"
print(json.dumps({"channel": "gcs-reader", "status": "valid"}))


def check_auth(args, kind, mode, source_name):
    rendered = subprocess.check_output(["helm", "template", "test", str(chart), *args], text=True)
    resources = list(yaml.safe_load_all(rendered))
    pod = next(item for item in resources if item["kind"] == kind)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert next(item for item in container["env"] if item["name"] == "GOOGLE_APPLICATION_CREDENTIALS")["value"] == "/var/run/google/credentials.json"
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert mounts["google-credentials"]["readOnly"]
    assert mounts["google-credentials"]["mountPath"] == "/var/run/google"
    if mode == "secret":
        assert volumes["google-credentials"]["secret"]["secretName"] == source_name
        assert "google-token" not in volumes
    else:
        assert volumes["google-credentials"]["configMap"]["name"] == source_name
        assert volumes["google-token"]["projected"]["sources"][0]["serviceAccountToken"] == {
            "audience": "https://iam.googleapis.com/example",
            "expirationSeconds": 3600,
            "path": "token",
        }
        assert mounts["google-token"]["mountPath"] == "/var/run/service-account"


for mode, settings, source in (
    ("secret", ["secretName=gcs-writer"], "gcs-writer"),
    ("federation", ["configMapName=gcs-writer-identity", "audience=https://iam.googleapis.com/example"], "gcs-writer-identity"),
):
    collector_overrides = [f"auth.mode={mode}", *(f"auth.{setting}" for setting in settings)]
    collector_args = [value for override in collector_overrides for value in ("--set", override)]
    check_auth(collector_args, "Deployment", mode, source)
    reader_overrides = [f"reader.auth.mode={mode}", *(f"reader.auth.{setting}" for setting in settings)]
    reader_auth_args = reader_args[4:] + [value for override in reader_overrides for value in ("--set", override)]
    check_auth(reader_auth_args, "StatefulSet", mode, source)
    for missing in ("secretName",) if mode == "secret" else ("configMapName", "audience"):
        invalid = ["helm", "template", "test", str(chart), "--set", f"auth.mode={mode}"]
        for setting in settings:
            if not setting.startswith(f"{missing}="):
                invalid += ["--set", f"auth.{setting}"]
        assert subprocess.run(invalid, capture_output=True).returncode != 0
        invalid_reader = list(reader_args) + ["--set", f"reader.auth.mode={mode}"]
        for setting in settings:
            if not setting.startswith(f"{missing}="):
                invalid_reader += ["--set", f"reader.auth.{setting}"]
        assert subprocess.run(invalid_reader, capture_output=True).returncode != 0
print(json.dumps({"channel": "gcs-auth", "status": "valid"}))
