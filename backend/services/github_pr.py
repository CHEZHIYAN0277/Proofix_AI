from pathlib import Path

from backend.config import Settings, get_settings
from backend.models.proof import VerificationBundle
from backend.services.git_service import get_head_sha
from backend.services.proof_bundle import (
    generate_verification_workflow,
    proof_bundle_relative_path,
    workflow_relative_path,
)


class GitHubPRService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def format_pr_body_with_proof(
        self,
        bundle: VerificationBundle,
        why_section: str,
        what_section: str,
        review_section: str = "",
    ) -> str:
        before = next(s for s in bundle.steps if s.name == "reproduction_before")
        after = next(s for s in bundle.steps if s.name == "reproduction_after")

        if bundle.reproduction_confidence == "exact_test":
            verify_header = (
                "## Verify this fix yourself\n"
                "This PR ships its own proof pinned to the **exact failing test**. "
                "Run the commands below, or wait for the automated check — nobody has to take our word for it.\n"
            )
            before_label = f"**Before (base commit `{before.base_commit[:12]}`) — exact failing test:**"
            after_label = f"**After (patch commit `{after.patch_commit[:12]}`) — same test:**"
        else:
            verify_header = (
                "## Verify this fix yourself\n"
                "This PR ships reproducible proof, but **no single failing test was identified** — "
                "verification runs the **full test suite** and observes overall pass/fail. "
                "This is lower-confidence than an exact-test pin.\n"
            )
            before_label = f"**Before (base commit `{before.base_commit[:12]}`) — full suite (expect failures):**"
            after_label = f"**After (patch commit `{after.patch_commit[:12]}`) — full suite (expect pass):**"

        body = (
            f"{verify_header}\n"
            f"{before_label}\n"
            f"```bash\n{before.command}\n```\n\n"
            f"{after_label}\n"
            f"```bash\n{after.command}\n```\n\n"
            "Automated verification: see the Checks tab — runs on GitHub's runners, zero LLM calls.\n\n"
            f"Proof bundle hash: `{bundle.bundle_hash}`\n\n"
            "---\n\n"
            f"## Why\n{why_section}\n\n"
            f"## What Changed\n{what_section}\n"
        )
        if review_section:
            body += f"\n## Review Note\n{review_section}\n"
        body += (
            "\n---\n\n"
            "## Reviewability (secondary)\n"
            "See axis scores in pipeline output — secondary signal only; proof above is authoritative.\n"
        )
        return body

    def publish_fix(
        self,
        repo_path: str,
        branch: str,
        patch_files: dict[str, str],
        commit_message: str,
        title: str,
        body: str,
        draft: bool = False,
        extra_files: dict[str, str] | None = None,
    ) -> str | None:
        """Create branch, apply patches + proof artifacts, commit, push, then open PR."""
        all_files = dict(patch_files)
        if extra_files:
            all_files.update(extra_files)

        if all_files and not self.create_branch_and_commit(
            repo_path, branch, all_files, commit_message, amend_with_proof=True
        ):
            return None
        return self.create_pr(title=title, body=body, branch=branch, draft=draft)

    def publish_fix_with_proof(
        self,
        repo_path: str,
        branch: str,
        patch_files: dict[str, str],
        bundle: VerificationBundle,
        commit_message: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> tuple[str | None, VerificationBundle]:
        """
        Commit patches, amend with proof bundle + workflow containing final patch SHA.
        Returns (pr_url, final_bundle).
        """
        if self.settings.github_dry_run or not self.settings.github_token:
            workflow_yaml = generate_verification_workflow(bundle)
            extra = {
                proof_bundle_relative_path(bundle): _bundle_json(bundle),
                workflow_relative_path(bundle): workflow_yaml,
            }
            url = self.publish_fix(
                repo_path, branch, patch_files, commit_message, title, body, draft, extra
            )
            return url, bundle

        try:
            from git import Repo

            repo = Repo(repo_path)
            self._checkout_branch(repo, branch)
            root = Path(repo_path)

            for path, content in patch_files.items():
                p = root / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                repo.index.add([path])

            if repo.is_dirty(untracked_files=True):
                repo.index.commit(commit_message)

            patch_sha = get_head_sha(root)
            for step in bundle.steps:
                step.patch_commit = patch_sha
            from backend.services.proof_bundle import compute_bundle_hash

            bundle.bundle_hash = compute_bundle_hash(bundle.steps)

            workflow_yaml = generate_verification_workflow(bundle)
            proof_path = proof_bundle_relative_path(bundle)
            workflow_path = workflow_relative_path(bundle)

            proof_abs = root / proof_path
            proof_abs.parent.mkdir(parents=True, exist_ok=True)
            proof_abs.write_text(_bundle_json(bundle), encoding="utf-8")

            workflow_abs = root / workflow_path
            workflow_abs.parent.mkdir(parents=True, exist_ok=True)
            workflow_abs.write_text(workflow_yaml, encoding="utf-8")

            repo.index.add([proof_path, workflow_path])
            repo.index.commit("Add proof-of-fix verification bundle", amend=True)
            origin = repo.remote(name="origin")
            origin.push(refspec=f"{branch}:{branch}")

            url = self.create_pr(title=title, body=body, branch=branch, draft=draft)
            return url, bundle
        except Exception:
            return None, bundle

    def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        draft: bool = False,
    ) -> str | None:
        owner = self.settings.github_repo_owner
        name = self.settings.github_repo_name

        if self.settings.github_dry_run or not self.settings.github_token:
            # A dry-run URL that names an unconfigured target is a guess about
            # where this PR would have gone. Say there is no target instead.
            if not owner or not name:
                return None
            return f"https://github.com/{owner}/{name}/pull/DRY_RUN"

        if not owner or not name:
            # Refusing beats opening a pull request against whatever repository
            # the defaults happen to name.
            return None

        from github import Github

        g = Github(self.settings.github_token)
        repo = g.get_repo(f"{owner}/{name}")
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base="main",
            draft=draft,
        )
        return pr.html_url

    def create_branch_and_commit(
        self,
        repo_path: str,
        branch: str,
        files: dict[str, str],
        message: str,
        amend_with_proof: bool = False,
    ) -> bool:
        if self.settings.github_dry_run:
            return True
        if not self.settings.github_token:
            return False
        if not files:
            return True
        try:
            from git import Repo

            repo = Repo(repo_path)
            self._checkout_branch(repo, branch)

            root = Path(repo_path)
            for path, content in files.items():
                p = root / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                repo.index.add([path])
            if repo.is_dirty(untracked_files=True):
                repo.index.commit(message)
                if not amend_with_proof:
                    origin = repo.remote(name="origin")
                    origin.push(refspec=f"{branch}:{branch}")
            return True
        except Exception:
            return False

    def _checkout_branch(self, repo, branch: str) -> None:
        if repo.head.is_detached:
            repo.git.checkout("-B", branch)
            return
        existing = [h.name for h in repo.heads]
        if branch in existing:
            repo.git.checkout(branch)
        else:
            repo.git.checkout("-b", branch)


def _bundle_json(bundle: VerificationBundle) -> str:
    import json

    return json.dumps(bundle.model_dump(mode="json"), indent=2, default=str)
