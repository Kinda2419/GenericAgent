# GenericAgent Quality Gate

This is a deterministic first pass for GenericAgent deliverable acceptance.

It addresses the quality-decay problem in a TL -> leader -> worker team:

- workers must attach evidence instead of only conclusions
- leaders can reject incomplete handoffs before summarizing
- TL can report status from checks, not from trust alone
- model-judged rubrics can be added later without replacing hard checks

## Recommended external projects

Use these as optional layers, not as the first dependency:

- `promptfoo`: CI-friendly prompt/agent regression suites and red-team checks.
- `DeepEval`: model-judged metrics such as task completion, relevance, and custom G-Eval rubrics.
- `Langfuse` or `Phoenix`: trace storage, eval datasets, run history, and drift monitoring.
- `Guardrails AI`: stronger schema/output validation when JSON contracts become complex.
- `Ragas`: RAG/report-specific faithfulness and context quality checks.

## Artifact format

Workers or leaders should write a JSON handoff:

```json
{
  "type": "code_change",
  "task": "Fix session routing",
  "status": "Done",
  "summary": "Session state now routes by topic id.",
  "evidence": [
    {"type": "file", "path": "frontends/feishu_sessions.py"},
    {"type": "test", "command": "py -m pytest tests/test_feishu_session_queue.py", "result": "passed"}
  ],
  "rubric_scores": {
    "correctness": 0.9,
    "verification": 0.9,
    "scope_control": 0.8,
    "maintainability": 0.8,
    "rollback_clarity": 0.7
  },
  "risk_next": "None"
}
```

Run:

```bash
py -m quality_gate.gate path/to/artifact.json --base-dir .
```

Machine-readable output:

```bash
py -m quality_gate.gate path/to/artifact.json --base-dir . --json
```

## Selection logic

Start with this local gate because GenericAgent is intentionally lightweight and self-evolving. Heavy eval stacks should be attached at the boundary where they add clear value:

- hard format/evidence/status checks stay local and deterministic
- promptfoo runs curated regression suites before release
- DeepEval grades subjective quality only after hard checks pass
- Langfuse/Phoenix records traces and builds a goldens dataset over time

