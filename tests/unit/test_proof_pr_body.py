"""PR body copy reflects reproduction confidence honestly."""

from backend.models.proof import VerificationBundle, VerificationStep
from backend.services.github_pr import GitHubPRService


def _bundle(confidence: str, targeted: bool) -> VerificationBundle:
    steps = [
        VerificationStep(
            name="reproduction_before",
            command="python -m pytest tests/t.py::test_x -v --tb=long",
            base_commit="a" * 40,
            patch_commit="b" * 40,
            expected_result="exit code 1",
            timeout_seconds=120,
            is_targeted=targeted,
        ),
        VerificationStep(
            name="reproduction_after",
            command="python -m pytest tests/t.py::test_x -v --tb=long",
            base_commit="a" * 40,
            patch_commit="b" * 40,
            expected_result="exit code 0",
            timeout_seconds=120,
            is_targeted=targeted,
        ),
    ]
    return VerificationBundle(
        issue_id="finding-1",
        steps=steps,
        bundle_hash="hash",
        reproduction_confidence=confidence,  # type: ignore[arg-type]
    )


def test_pr_body_exact_test_wording():
    body = GitHubPRService().format_pr_body_with_proof(
        _bundle("exact_test", True), "why", "what"
    )
    assert "exact failing test" in body
    assert "lower-confidence" not in body


def test_pr_body_full_suite_wording():
    body = GitHubPRService().format_pr_body_with_proof(
        _bundle("full_suite", False), "why", "what"
    )
    assert "full test suite" in body
    assert "lower-confidence" in body
    assert "exact failing test" not in body.split("Reviewability")[0]
