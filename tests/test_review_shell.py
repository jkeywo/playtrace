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


def test_phase2_review_markup_exposes_experiment_applicability_and_decision() -> None:
    payload = {
        "snapshot": {"id": "snapshot:1"},
        "analysis": {"entity_count": 1, "semantic_digest": "digest"},
        "review": {
            "intent": {
                "kind": "experiment",
                "desired_outcome": "Test tension",
                "original_prompt": "Test tension.",
                "scope": [],
            },
            "decision": {
                "state": "revise",
                "rationale": "Retest the policy.",
                "decided_by": "designer",
                "decided_at": "2026-08-26T10:00:00Z",
            },
            "experiment_evidence": [
                {
                    "kind": "ab_comparison",
                    "summary": "Candidate did not improve flow.",
                    "applicability": "stale",
                    "applicability_reasons": ["source revision changed"],
                    "hypothesis_link": {
                        "hypothesis_id": "hypothesis:tension",
                        "relation": "contradicts",
                    },
                }
            ],
            "coverage": {"complete": True, "items": []},
            "claims": [],
            "hypotheses": [],
            "critic_findings": [],
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
        "if (!html.includes('Candidate did not improve flow.') || "
        "!html.includes('applicability-stale') || "
        "!html.includes('decision-revise')) process.exit(1);"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_review_markup_links_proposals_regressions_and_inert_extensions_read_only() -> None:
    payload = {
        "snapshot": {"id": "snapshot:1"},
        "analysis": {"entity_count": 1, "semantic_digest": "digest"},
        "review": {
            "intent": {"kind": "change_request", "desired_outcome": "Change", "original_prompt": "Change", "scope": []},
            "coverage": {"complete": True, "items": []},
            "claims": [], "hypotheses": [], "critic_findings": [], "policies": [],
            "critic_routes": [], "alternatives": [], "limitations": [],
        },
        "proposals": [{
            "proposal": {"id": "proposal:1", "title": "A proposal", "question": "Change it?", "state": "approved", "basis_digest": "abc"},
            "attempts": [],
        }],
        "regression_runs": [{"suite_id": "suite:1", "passed": True, "digest": "def"}],
        "extension_proposals": [{"title": "Suggested grammar", "kind": "cnl_grammar"}],
    }
    module = Path("src/playtrace/review/app.js").resolve().as_uri()
    source = (
        f'import {{ reviewMarkup }} from "{module}"; '
        f"const html = reviewMarkup({json.dumps(payload)}); "
        "if (!html.includes('A proposal') || !html.includes('suite:1') || "
        "!html.includes('Suggested grammar') || !html.includes('READ ONLY')) process.exit(1);"
    )
    result = subprocess.run(["node", "--input-type=module", "-e", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
