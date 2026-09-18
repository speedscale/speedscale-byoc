#!/usr/bin/env python3
"""Run partner RRPair transforms with real protocol shapes in the pinned collector."""

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHARTS = ("datadog", "newrelic", "dynatrace")


def otlp_value(item):
    if isinstance(item, dict):
        return {
            "kvlistValue": {
                "values": [
                    {"key": key, "value": otlp_value(value)}
                    for key, value in item.items()
                ]
            }
        }
    if isinstance(item, list):
        return {"arrayValue": {"values": [otlp_value(value) for value in item]}}
    if isinstance(item, bool):
        return {"boolValue": item}
    if isinstance(item, int):
        return {"intValue": str(item)}
    return {"stringValue": str(item)}


def decoded_value(value):
    if "kvlistValue" in value:
        return {
            item["key"]: decoded_value(item["value"])
            for item in value["kvlistValue"].get("values", [])
        }
    if "arrayValue" in value:
        return [decoded_value(item) for item in value["arrayValue"].get("values", [])]
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    return None


def attribute_map(items):
    return {item["key"]: decoded_value(item["value"]) for item in items}


RECORDS = [
    {
        "msgType": "rrpair",
        "l7protocol": "https",
        "service": "banking-ai",
        "direction": "OUT",
        "command": "POST",
        "status": "200",
        "netinfo": {"upstream": {"hostname": "api.anthropic.com", "port": 443}},
        "http": {
            "req": {
                "method": "POST",
                "uri": "/v1/messages",
                "headers": {
                    "Traceparent": [
                        "00-11111111111111111111111111111111-2222222222222222-01"
                    ]
                },
            },
            "res": {"statusCode": 200},
        },
    },
    {
        "msgType": "rrpair",
        "l7protocol": "postgres",
        "service": "banking-accounts",
        "direction": "OUT",
        "command": "Execute Prepared Statement",
        "status": "OK",
        "netinfo": {
            "upstream": {
                "hostname": "banking-postgres.banking-app.svc.cluster.local",
                "port": 5432,
            }
        },
        "postgres": {"request": {"execute": {}}, "response": {"execute": {}}},
    },
    {
        "msgType": "rrpair",
        "l7protocol": "kafka",
        "service": "banking-notification",
        "direction": "OUT",
        "command": "Fetch",
        "status": "OK",
        "netinfo": {
            "upstream": {
                "hostname": "banking-kafka.banking-app.svc.cluster.local",
                "port": 9092,
            }
        },
        "kafka": {"request": {}, "response": {}},
    },
]

PAYLOAD = {
    "resourceLogs": [
        {
            "resource": {},
            "scopeLogs": [
                {
                    "scope": {"name": "speedscale/rrpair"},
                    "logRecords": [
                        {
                            "timeUnixNano": str(time.time_ns() + offset),
                            "body": otlp_value(record),
                        }
                        for offset, record in enumerate(RECORDS)
                    ],
                }
            ],
        }
    ]
}

EXPECTED = {
    "banking-ai": {
        "destination": "api.anthropic.com",
        "protocol": "https",
        "command": "POST",
        "status": "200",
        "summary": "OUT POST /v1/messages",
        "trace_id": "11111111111111111111111111111111",
    },
    "banking-accounts": {
        "destination": "banking-postgres.banking-app.svc.cluster.local",
        "protocol": "postgres",
        "command": "Execute Prepared Statement",
        "status": "OK",
        "summary": "OUT postgres Execute Prepared Statement",
        "trace_id": None,
    },
    "banking-notification": {
        "destination": "banking-kafka.banking-app.svc.cluster.local",
        "protocol": "kafka",
        "command": "Fetch",
        "status": "OK",
        "summary": "OUT kafka Fetch",
        "trace_id": None,
    },
}


def rendered_collector(chart):
    rendered = subprocess.check_output(
        ["helm", "template", "capture-test", str(ROOT / "charts" / chart)],
        text=True,
    )
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    configmap = next(
        item
        for item in objects
        if item.get("kind") == "ConfigMap" and "otel.yaml" in item.get("data", {})
    )
    image = next(
        container["image"]
        for item in objects
        if item.get("kind") == "Deployment"
        for container in item["spec"]["template"]["spec"]["containers"]
        if container["name"] == "collector"
    )
    return yaml.safe_load(configmap["data"]["otel.yaml"]), image


def wait_for_collector(port):
    last_error = None
    for _ in range(30):
        try:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/logs",
                    data=json.dumps(PAYLOAD).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=2,
            )
            if response.status == 200:
                return
        except (OSError, urllib.error.HTTPError) as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"collector rejected test data: {last_error}")


def assert_output(chart, output_path):
    exported = [json.loads(line) for line in output_path.read_text().splitlines()]
    output = {}
    for batch in exported:
        for resource_logs in batch.get("resourceLogs", []):
            resource = attribute_map(resource_logs.get("resource", {}).get("attributes", []))
            for scope in resource_logs.get("scopeLogs", []):
                for log in scope.get("logRecords", []):
                    attributes = attribute_map(log.get("attributes", []))
                    output[attributes["speedscale.workload"]] = {
                        "body": decoded_value(log["body"]),
                        "attributes": attributes,
                        "resource": resource,
                        "trace_id": log.get("traceId"),
                    }

    assert set(output) == set(EXPECTED), output
    for workload, wanted in EXPECTED.items():
        actual = output[workload]
        attributes = actual["attributes"]
        assert actual["resource"]["service.name"] == workload, actual
        assert attributes["hostname"] == wanted["destination"], actual
        assert attributes["server.address"] == wanted["destination"], actual
        assert attributes["network.peer.address"] == wanted["destination"], actual
        assert attributes["msgType"] == "rrpair", actual
        assert attributes["speedscale.protocol"] == wanted["protocol"], actual
        assert attributes["speedscale.command"] == wanted["command"], actual
        assert attributes["speedscale.status"] == wanted["status"], actual
        assert actual["trace_id"] == wanted["trace_id"], actual
        if chart == "datadog":
            assert actual["body"]["msgType"] == "rrpair", actual
            assert actual["body"]["netinfo"]["upstream"]["hostname"] == wanted["destination"], actual
        else:
            assert actual["body"] == wanted["summary"], actual


def main():
    for offset, chart in enumerate(CHARTS):
        config, image = rendered_collector(chart)
        pipeline = config["service"]["pipelines"]["logs"]
        pipeline["exporters"] = ["file/test"]
        config["service"]["pipelines"] = {"logs": pipeline}
        config["exporters"] = {"file/test": {"path": "/test/output.json"}}
        config.pop("connectors", None)

        with tempfile.TemporaryDirectory(prefix=f"capture-transform-{chart}-") as folder:
            directory = Path(folder)
            (directory / "config.yaml").write_text(yaml.safe_dump(config))
            port = 15430 + offset
            collector = subprocess.check_output(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--user",
                    "0",
                    "-p",
                    f"127.0.0.1:{port}:4318",
                    "-v",
                    f"{directory}:/test",
                    image,
                    "--config=/test/config.yaml",
                ],
                text=True,
            ).strip()
            collector_logs = ""
            try:
                wait_for_collector(port)
                time.sleep(2)
                collector_logs = subprocess.check_output(
                    ["docker", "logs", collector], stderr=subprocess.STDOUT, text=True
                )
            finally:
                subprocess.run(
                    ["docker", "stop", collector],
                    stdout=subprocess.DEVNULL,
                    check=True,
                )

            assert "failed processing logs" not in collector_logs, collector_logs
            assert_output(chart, directory / "output.json")
            print(f"validated {chart} capture transforms for HTTPS, PostgreSQL, and Kafka")


if __name__ == "__main__":
    main()
