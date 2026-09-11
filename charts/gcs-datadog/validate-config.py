#!/usr/bin/env python3
"""Validate both chart modes with the pinned collector binary (requires PyYAML and Docker)."""
import json
import subprocess
import tempfile
from pathlib import Path

import yaml

chart = Path(__file__).resolve().parent
for enabled in (True, False):
    rendered = subprocess.check_output(["helm", "template", "test", str(chart), "--set", f"datadog.enabled={str(enabled).lower()}"], text=True)
    objects = list(yaml.safe_load_all(rendered))
    config = next(item for item in objects if item["kind"] == "ConfigMap")["data"]["otel.yaml"]
    image = next(item for item in objects if item["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["image"]
    with tempfile.TemporaryDirectory() as folder:
        Path(folder).chmod(0o755)
        Path(folder, "otel.yaml").write_text(config)
        Path(folder, "otel.yaml").chmod(0o644)
        subprocess.run(["docker", "run", "--rm", "--network=none", "-e", "DD_API_KEY=" + "0" * 32, "-v", f"{folder}:/conf:ro", image, "validate", "--config=/conf/otel.yaml"], check=True)
    print(json.dumps({"datadog": enabled, "status": "valid"}))
