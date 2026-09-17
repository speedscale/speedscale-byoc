#!/usr/bin/env python3
"""Validate primary collector configs with each chart's pinned image."""

import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "datadog": ["-e", "DD_API_KEY=" + "0" * 32],
    "dynatrace": ["-e", "DT_API_TOKEN=dt0c01.validation"],
    "newrelic": ["-e", "NEW_RELIC_LICENSE_KEY=validation"],
    "gcs": [],
}


def main():
    for chart, environment in ENV.items():
        rendered = subprocess.check_output(
            ["helm", "template", "collector-validation", str(ROOT / "charts" / chart)],
            text=True,
        )
        objects = list(yaml.safe_load_all(rendered))
        config = next(item for item in objects if item.get("kind") == "ConfigMap" and "otel.yaml" in item.get("data", {}))["data"]["otel.yaml"]
        image = next(item for item in objects if item.get("kind") == "Deployment")["spec"]["template"]["spec"]["containers"][0]["image"]
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder, "otel.yaml")
            config_path.write_text(config)
            config_path.chmod(0o644)
            Path(folder).chmod(0o755)
            subprocess.run(
                ["docker", "run", "--rm", "--network=none", *environment, "-v", f"{folder}:/conf:ro", image, "validate", "--config=/conf/otel.yaml"],
                check=True,
            )
        print(f"validated {chart} collector with {image}")


if __name__ == "__main__":
    main()
