import argparse
import json
import os
import sys
from dataclasses import dataclass


ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STANDARDS = os.path.join(ROOT, "standards.json")


GRADE_FLOORS = {
    "A": 0.9,
    "B": 0.8,
    "C": 0.65,
    "D": 0.5,
    "F": 0.0,
}


@dataclass
class Finding:
    level: str
    code: str
    message: str

    def as_dict(self):
        return {"level": self.level, "code": self.code, "message": self.message}


def _load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _score_to_grade(score):
    if score >= GRADE_FLOORS["A"]:
        return "A"
    if score >= GRADE_FLOORS["B"]:
        return "B"
    if score >= GRADE_FLOORS["C"]:
        return "C"
    if score >= GRADE_FLOORS["D"]:
        return "D"
    return "F"


def _normalize_status(value):
    return str(value or "").strip().lower().replace("_", " ")


def _evidence_exists(evidence, base_dir):
    path = evidence.get("path")
    if not path:
        return False
    if os.path.isabs(path):
        return os.path.exists(path)
    return os.path.exists(os.path.join(base_dir, path))


def evaluate(artifact, standards, base_dir):
    deliverable_type = artifact.get("type")
    profile = standards.get("deliverable_types", {}).get(deliverable_type)
    findings = []

    if not profile:
        findings.append(
            Finding(
                "fail",
                "unknown_type",
                f"Unknown deliverable type: {deliverable_type!r}.",
            )
        )
        return _result(artifact, findings, 0.0, "F", False)

    for field in standards.get("required_fields", []):
        if artifact.get(field) in (None, "", []):
            findings.append(
                Finding("fail", "missing_field", f"Missing required field: {field}.")
            )

    status = _normalize_status(artifact.get("status"))
    evidence_items = artifact.get("evidence") or []
    if status == "done":
        verification = artifact.get("verification") or []
        verification_types = {"test", "smoke", "browser_check", "claim_check"}
        has_verification_evidence = any(
            item.get("type") in verification_types for item in evidence_items
        )
        if not verification and not has_verification_evidence:
            findings.append(
                Finding(
                    "fail",
                    "done_without_verification",
                    "Status is Done but no verification evidence is attached.",
                )
            )
        if artifact.get("risk_next") in (None, ""):
            findings.append(
                Finding(
                    "warn",
                    "missing_risk_next",
                    "Done deliverables should explicitly state Risk/Next, even when it is None.",
                )
            )

    required_evidence = profile.get("required_evidence", [])
    evidence_types = {item.get("type") for item in evidence_items}
    for evidence_type in required_evidence:
        if evidence_type not in evidence_types:
            findings.append(
                Finding(
                    "fail",
                    "missing_evidence",
                    f"Missing required evidence type: {evidence_type}.",
                )
            )

    for item in evidence_items:
        if item.get("type") == "file" and not _evidence_exists(item, base_dir):
            findings.append(
                Finding(
                    "fail",
                    "missing_file_evidence",
                    f"Evidence file does not exist: {item.get('path')!r}.",
                )
            )

    rubric_scores = artifact.get("rubric_scores") or {}
    weighted_total = 0.0
    weight_sum = 0.0
    for key, dimension in profile.get("rubric", {}).items():
        weight = float(dimension.get("weight", 1))
        score = rubric_scores.get(key)
        if score is None:
            findings.append(
                Finding("fail", "missing_rubric_score", f"Missing rubric score: {key}.")
            )
            score = 0
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            findings.append(
                Finding("fail", "bad_rubric_score", f"Rubric score is not numeric: {key}.")
            )
            numeric = 0
        numeric = max(0.0, min(1.0, numeric))
        weighted_total += numeric * weight
        weight_sum += weight

    score = weighted_total / weight_sum if weight_sum else 0.0
    grade = _score_to_grade(score)
    min_grade = profile.get("minimum_grade", standards.get("default_minimum_grade", "B"))
    passed = not any(f.level == "fail" for f in findings)
    passed = passed and score >= GRADE_FLOORS.get(min_grade, GRADE_FLOORS["B"])

    if not passed and not any(f.level == "fail" for f in findings):
        findings.append(
            Finding(
                "fail",
                "below_grade_floor",
                f"Score {score:.2f} produced grade {grade}, below required grade {min_grade}.",
            )
        )

    return _result(artifact, findings, score, grade, passed)


def _result(artifact, findings, score, grade, passed):
    return {
        "passed": passed,
        "type": artifact.get("type"),
        "status": artifact.get("status"),
        "score": round(score, 4),
        "grade": grade,
        "findings": [finding.as_dict() for finding in findings],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a deterministic quality gate.")
    parser.add_argument("artifact", help="Path to a deliverable metadata JSON file.")
    parser.add_argument("--standards", default=DEFAULT_STANDARDS)
    parser.add_argument("--base-dir", default=os.getcwd())
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    artifact = _load_json(args.artifact)
    standards = _load_json(args.standards)
    result = evaluate(artifact, standards, args.base_dir)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        state = "PASS" if result["passed"] else "FAIL"
        print(f"{state} type={result['type']} grade={result['grade']} score={result['score']}")
        for finding in result["findings"]:
            print(f"- {finding['level']} {finding['code']}: {finding['message']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
