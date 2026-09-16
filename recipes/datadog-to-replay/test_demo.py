import copy
import json
from pathlib import Path
import tempfile
import unittest

import demo


class DemoTests(unittest.TestCase):
    def test_unrelated_failure_does_not_pass_fault_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict.json"
            pair = {
                "recordedStatus": 200,
                "observedStatus": 404,
                "bodyMatch": "pass",
                "match": "fail",
            }
            path.write_text(json.dumps({"pairs": [pair]}))
            self.assertFalse(demo.verdict(path, "fault", 1)["expected_behavior"])
            pair["observedStatus"] = 503
            path.write_text(json.dumps({"pairs": [pair]}))
            self.assertTrue(demo.verdict(path, "fault", 1)["expected_behavior"])
            self.assertFalse(demo.verdict(path, "fault", 0)["expected_behavior"])

    def test_baseline_requires_body_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict.json"
            pair = {
                "recordedStatus": 200,
                "observedStatus": 200,
                "bodyMatch": "fail",
                "match": "pass",
            }
            path.write_text(json.dumps({"pairs": [pair]}))
            self.assertFalse(demo.verdict(path, "baseline", 0)["expected_behavior"])

    def test_local_auth_preserves_response_and_original(self):
        token = (
            demo.encode({"alg": "HS256"})
            + "."
            + demo.encode({"sub": "demo", "exp": 1})
            + ".old"
        )
        original = {
            "http": {
                "req": {"headers": {"Authorization": ["Bearer " + token]}},
                "res": {"statusCode": 200, "bodyBase64": "e30="},
            }
        }
        local = copy.deepcopy(original)
        demo.local_auth(local, "isolated-demo-secret")
        self.assertNotEqual(local["http"]["req"], original["http"]["req"])
        self.assertEqual(local["http"]["res"], original["http"]["res"])
        self.assertEqual(
            original["http"]["req"]["headers"]["Authorization"], ["Bearer " + token]
        )


if __name__ == "__main__":
    unittest.main()


class EmptyGetNormalizationTests(unittest.TestCase):
    def test_only_known_empty_body_marker_is_removed(self):
        import base64
        def record(body, method="GET", modified=True):
            return {"dlpModified": modified, "http": {"req": {
                "method": method, "bodyBase64": base64.b64encode(json.dumps(body).encode()).decode()
            }, "res": {"statusCode": 200, "body": "unchanged"}}}
        marker = {"$api_key": "REDACTED-UNRECOGNIZED-e3b0c44298fc1c149afb"}
        sample = record(marker)
        self.assertTrue(demo.normalize_empty_get(sample))
        self.assertNotIn("bodyBase64", sample["http"]["req"])
        self.assertEqual(sample["http"]["res"]["body"], "unchanged")
        for sample in [record(marker, "POST"), record(marker, modified=False),
                       record({"$api_key": "another-value"}), record({"balance": 1})]:
            self.assertFalse(demo.normalize_empty_get(sample))
            self.assertIn("bodyBase64", sample["http"]["req"])
