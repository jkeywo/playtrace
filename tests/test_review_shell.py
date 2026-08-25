import subprocess
import json
from pathlib import Path

from playtrace.demo import build_demo_review


def test_review_payload_contains_traceable_claim() -> None:
    payload = build_demo_review()

    assert payload["claim"]["evidence"]
    assert payload["analysis"]["semantic_digest"]
    assert payload["snapshot"]["source_revisions"]


def test_review_javascript_parses() -> None:
    script = Path("src/playtrace/review/app.js")
    result = subprocess.run(
        ["node", "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_phase2_review_markup_exposes_claim_coverage_and_judgment() -> None:
    payload = {
        "snapshot": {"id": "snapshot:1"},
        "analysis": {"entity_count": 1, "semantic_digest": "digest"},
        "review": {
            "intent": {
                "id": "intent:1",
                "kind": "diagnosis",
                "desired_outcome": "Find the blocker",
                "original_prompt": "Review this.",
                "scope": ["entity:1"],
            },
            "coverage": {
                "complete": True,
                "items": [
                    {
                        "status": "covered",
                        "target_id": "policy:1",
                        "reason": "mandatory",
                        "covered_by": ["claim:1"],
                    }
                ],
            },
            "claims": [
                {
                    "id": "claim:1",
                    "statement": "A material claim.",
                    "evidence": [
                        {
                            "relation": "supports",
                            "evidence_id": "evidence:1",
                            "rationale": "Observed.",
                        }
                    ],
                }
            ],
            "hypotheses": [],
            "critic_findings": [
                {
                    "critic_id": "critic:experience",
                    "stance": "risk",
                    "title": "Unknown feel",
                    "statement": "Not observed.",
                    "unsupported_judgment": True,
                    "claim_ids": [],
                }
            ],
            "policies": [],
            "critic_routes": [],
            "alternatives": [],
            "limitations": [],
        },
    }
    module = Path("src/playtrace/review/app.js").resolve().as_uri()
    source = (
        f'import {{ reviewMarkup }} from "{module}"; '
        f"const html = reviewMarkup({json.dumps(payload)}); "
        "if (!html.includes('A material claim.') || "
        "!html.includes('UNSUPPORTED JUDGMENT') || "
        "!html.includes('coverage-covered')) process.exit(1);"
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
