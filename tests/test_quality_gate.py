import json
import os
import tempfile
import unittest

from quality_gate.gate import evaluate


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STANDARDS_PATH = os.path.join(ROOT, "quality_gate", "standards.json")


def _standards():
    with open(STANDARDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class QualityGateTests(unittest.TestCase):
    def test_code_change_with_evidence_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_file = os.path.join(tmp, "changed.py")
            with open(evidence_file, "w", encoding="utf-8") as f:
                f.write("print('ok')\n")

            artifact = {
                "type": "code_change",
                "task": "Create a checked artifact.",
                "status": "Done",
                "summary": "A small file was created and tested.",
                "evidence": [
                    {"type": "file", "path": "changed.py"},
                    {"type": "test", "command": "unit", "result": "passed"},
                ],
                "rubric_scores": {
                    "correctness": 0.9,
                    "verification": 0.9,
                    "scope_control": 0.8,
                    "maintainability": 0.8,
                    "rollback_clarity": 0.8,
                },
                "risk_next": "None",
            }

            result = evaluate(artifact, _standards(), tmp)

        self.assertTrue(result["passed"])
        self.assertEqual(result["grade"], "B")

    def test_done_without_verification_fails(self):
        artifact = {
            "type": "code_change",
            "task": "Pretend to finish.",
            "status": "Done",
            "summary": "No evidence was supplied.",
            "evidence": [],
            "rubric_scores": {
                "correctness": 1,
                "verification": 1,
                "scope_control": 1,
                "maintainability": 1,
                "rollback_clarity": 1,
            },
            "risk_next": "None",
        }

        result = evaluate(artifact, _standards(), ROOT)

        self.assertFalse(result["passed"])
        self.assertIn("done_without_verification", {f["code"] for f in result["findings"]})

    def test_missing_rubric_dimension_fails(self):
        artifact = {
            "type": "agent_workflow",
            "task": "Build a handoff flow.",
            "status": "Partially done",
            "summary": "Trace exists, but rubric is incomplete.",
            "evidence": [
                {"type": "trace", "path": "trace.json"},
                {"type": "smoke", "result": "passed"},
            ],
            "rubric_scores": {
                "role_boundary": 0.9,
                "non_silent_failure": 0.9,
            },
            "risk_next": "Missing calibration.",
        }

        result = evaluate(artifact, _standards(), ROOT)

        self.assertFalse(result["passed"])
        self.assertIn("missing_rubric_score", {f["code"] for f in result["findings"]})


if __name__ == "__main__":
    unittest.main()
