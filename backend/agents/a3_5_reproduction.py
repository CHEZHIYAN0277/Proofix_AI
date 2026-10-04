from datetime import datetime
from pathlib import Path

from backend.agents.base import AgentBase
from backend.models.reproduction import ReproductionStatus
from backend.services.reproduction_commands import build_reproduction_command
from backend.services.reproduction_parser import (
    extract_failed_nodeids,
    load_pytest_report,
    parse_pytest_report,
    pytest_report_path,
)
from backend.services.subprocess_runner import PYTHON, run_command
from backend.state.schema import RunStateModel

_STATUS_MESSAGES = {
    ReproductionStatus.CONFIRMED: "Reproduction confirmed",
    ReproductionStatus.UNCONFIRMED: "Reproduction NOT confirmed",
    ReproductionStatus.INFRA_ERROR: "Reproduction infrastructure error",
    ReproductionStatus.NO_TESTS: "No tests available for reproduction",
}


class A35ReproductionAgent(AgentBase):
    agent_id = "A3.5"

    async def run(self, state: RunStateModel) -> RunStateModel:
        await self.emit_status(state, "started", "Running reproduction gate via pytest")
        repo = Path(state.repo_clone_path or state.repo_path).resolve()
        report_path = pytest_report_path(state.run_id)

        if report_path.exists():
            report_path.unlink()

        cmd = [
            PYTHON,
            "-m",
            "pytest",
            "--tb=long",
            "--json-report",
            f"--json-report-file={report_path}",
            "-v",
        ]
        started_at = datetime.utcnow()
        code, stdout, stderr = await run_command(cmd, cwd=repo, timeout=120)
        finished_at = datetime.utcnow()

        report = load_pytest_report(report_path)
        result = parse_pytest_report(report, code, stdout, stderr, report_path, repo_root=repo)
        result.pre_existing_failures = extract_failed_nodeids(report)
        # The command that actually produced this evidence — every element
        # literal, no shell interpolation, safe to show and copy verbatim.
        result.command = "python -m pytest --tb=long --json-report -v"
        result.started_at = started_at.isoformat()
        result.finished_at = finished_at.isoformat()
        if result.duration_seconds is None:
            result.duration_seconds = (finished_at - started_at).total_seconds()

        reexec_cmd, is_targeted, reexec_timeout = build_reproduction_command(result.failing_test)
        result.reexecution_command = reexec_cmd
        result.reexecution_is_targeted = is_targeted
        result.reexecution_timeout_seconds = reexec_timeout

        result_dict = result.model_dump(mode="json")
        state.reproduction = result_dict
        # `state.force_draft_pr` is deliberately not set here. A3.5 reports what
        # it observed — the status is in `result` — and `trust_gating` derives
        # the routing consequence from it. See that module's docstring for why
        # three writers of one flag had no single moment at which it was true.

        await self.emit_status(
            state,
            "completed",
            _STATUS_MESSAGES.get(result.status, str(result.status)),
            {
                "status": result.status.value,
                "reproduced": result.reproduced,
                "force_draft_pr": result.force_draft_pr,
                "failing_test": result.failing_test,
                "report_path": result.report_path,
            },
        )
        return state
