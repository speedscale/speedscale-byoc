import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gather

TRACE = "a" * 32


def event(index, direction, trace=TRACE):
    record = {
        "msgType": "rrpair",
        "uuid": base64.b64encode(bytes([index]) * 16).decode(),
        "l7protocol": "http",
        "direction": direction,
        "http": {
            "req": {
                "method": "GET",
                "uri": "/api/accounts",
                "headers": {"Accept": '["application/json"]'},
            },
            "res": {
                "statusCode": 200,
                "bodyBase64": base64.b64encode(b'[{"balance":100}]').decode(),
            },
        },
    }
    return {
        "id": f"log-{index}",
        "attributes": {"attributes": {**record, "otel.trace_id": trace}},
    }


class GatherTests(unittest.TestCase):
    def test_spans_request_envelope_and_null_page(self):
        def request(url, headers, body):
            self.assertEqual(body["data"]["type"], "search_request")
            self.assertEqual(body["data"]["attributes"]["filter"]["query"], "trace_id:test")
            return json.dumps({"data": [{"id": "span"}], "meta": {"page": None}}).encode()

        with patch.object(gather, "request", side_effect=request):
            self.assertEqual(gather.search("datadoghq.com", {}, "spans", "trace_id:test"), [{"id": "span"}])

    def test_preserves_payload_and_restores_header_arrays(self):
        record = gather.event_record(event(1, "IN"))
        self.assertEqual(record["http"]["req"]["headers"]["Accept"], ["application/json"])
        self.assertEqual(record["http"]["res"]["statusCode"], 200)
        self.assertEqual(base64.b64decode(record["http"]["res"]["bodyBase64"]), b'[{"balance":100}]')

    def test_rejects_trace_without_test_and_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "incoming test traffic"):
                gather.write_capture(Path(tmp) / "capture", TRACE, "api-gateway", [event(1, "IN")], [{}])

    def test_rejects_mixed_trace_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "different trace"):
                gather.write_capture(
                    Path(tmp) / "capture",
                    TRACE,
                    "api-gateway",
                    [event(1, "IN"), event(2, "OUT", "b" * 32)],
                    [{}],
                )

    def test_query_writes_datadog_test_and_mock_without_gcs(self):
        def search(site, headers, signal, query):
            self.assertEqual(headers["DD-API-KEY"], "partner-test")
            self.assertIn(TRACE, query)
            if signal == "logs":
                return [event(1, "IN"), event(2, "OUT")]
            return [{"attributes": {"resource_name": "GET /api/accounts", "span_id": "123"}}]

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capture"
            argv = ["gather.py", "--trace-id", TRACE, "--out", str(out)]
            env = {
                "DATADOG_PARTNER_API_KEY": "partner-test",
                "DATADOG_PARTNER_APP_KEY": "partner-app-test",
                "DATADOG_PARTNER_SITE": "datadoghq.com",
            }
            with patch.dict(os.environ, env, clear=True), patch("sys.argv", argv), patch.object(gather, "search", side_effect=search):
                gather.main()
            self.assertEqual(len(list((out / "tests").glob("*.json"))), 1)
            self.assertEqual(len(list((out / "mocks").glob("*.json"))), 1)
            provenance = json.loads((out / "provenance.json").read_text())
            self.assertEqual(provenance["source"], "datadog")
            self.assertEqual(provenance["log_ids"], ["log-1", "log-2"])
            self.assertNotIn("gcs_objects", provenance)

    def test_partner_credentials_do_not_fall_back_to_default(self):
        argv = ["gather.py", "--trace-id", TRACE, "--out", "/tmp/unused"]
        env = {"DATADOG_API_KEY": "production", "DATADOG_APP_KEY": "production"}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", argv), self.assertRaises(SystemExit):
            gather.main()


if __name__ == "__main__":
    unittest.main()
