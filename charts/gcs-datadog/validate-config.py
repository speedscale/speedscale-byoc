#!/usr/bin/env python3
"""Validate chart profiles and the reader contract with the pinned collector binary (requires PyYAML and Docker)."""
import json
import subprocess
import tempfile
from pathlib import Path

import yaml

chart = Path(__file__).resolve().parent
for profile, enabled in (("archive", False), ("trace-correlation", True), ("trace-correlation", False)):
    rendered = subprocess.check_output(["helm", "template", "test", str(chart), "--set", f"datadog.enabled={str(enabled).lower()}", "--set", f"profile={profile}"], text=True)
    objects = list(yaml.safe_load_all(rendered))
    config = next(item for item in objects if item["kind"] == "ConfigMap")["data"]["otel.yaml"]
    parsed = yaml.safe_load(config)
    processors = parsed["service"]["pipelines"]["logs/gcs"]["processors"]
    if profile == "archive":
        assert "filter/traced" not in processors, "Generic archival must retain untraced traffic"
        assert "logs/correlation" not in parsed["service"]["pipelines"]
        assert "datadog" not in parsed["exporters"]
    else:
        assert "filter/traced" in processors
    image = next(item for item in objects if item["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["image"]
    with tempfile.TemporaryDirectory() as folder:
        Path(folder).chmod(0o755)
        Path(folder, "otel.yaml").write_text(config)
        Path(folder, "otel.yaml").chmod(0o644)
        subprocess.run(["docker", "run", "--rm", "--network=none", "-e", "DD_API_KEY=" + "0" * 32, "-v", f"{folder}:/conf:ro", image, "validate", "--config=/conf/otel.yaml"], check=True)
    print(json.dumps({"profile": profile, "datadog": enabled, "status": "valid"}))

reader_args = ["helm", "template", "test", str(chart), "--set", "reader.enabled=true", "--set", "reader.image=example/reader:validation", "--set", "reader.rbac.create=true", "--set", "reader.rbac.subjects[0].kind=Group", "--set", "reader.rbac.subjects[0].name=traffic-readers"]
objects = list(yaml.safe_load_all(subprocess.check_output(reader_args, text=True)))
reader = next(item for item in objects if item["kind"] == "StatefulSet")
collector = next(item for item in objects if item["kind"] == "Deployment")
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
print(json.dumps({"reader": "configuration-and-rbac", "status": "valid"}))
