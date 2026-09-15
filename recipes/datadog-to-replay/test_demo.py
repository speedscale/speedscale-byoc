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
