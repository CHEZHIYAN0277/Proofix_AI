from typing import Literal

from backend.config import get_settings
from backend.orchestrator.trust_gating import (
    MAX_REINVESTIGATIONS,
    mutation_validation_failed,
    security_validation_failed,
)


def should_reinvestigate(state: dict) -> Literal["investigate", "blast_scope"]:
    if state.get("reinvestigation_exhausted"):
        return "blast_scope"
    root_cause = state.get("root_cause") or {}
    if root_cause.get("evidence_incomplete"):
        return "blast_scope"
    count = root_cause.get("reinvestigation_count", 0)
    if root_cause.get("reinvestigation_required") and count <= MAX_REINVESTIGATIONS:
        return "investigate"
    return "blast_scope"


def after_mutation(state: dict) -> Literal["validate_security", "generate_code", "route_pr"]:
    mutation = state.get("mutation_result") or {}
    retry_count = state.get("retry_count", 0)
    max_retries = get_settings().max_retries

    if mutation.get("patch_retry_required"):
        if retry_count < max_retries:
            return "generate_code"
        return "route_pr"
    if mutation_validation_failed(mutation):
        return "route_pr"
    return "validate_security"


def after_security(state: dict) -> Literal["route_pr", "generate_code"]:
    security = state.get("security_result") or {}
    retry_count = state.get("retry_count", 0)
    max_retries = get_settings().max_retries

    if security_validation_failed(security):
        if retry_count < max_retries:
            return "generate_code"
    return "route_pr"


def after_environment(state: dict) -> Literal["reproduction_gate", "halt_environment"]:
    """Whether reproduction can realistically execute.

    The gate the pipeline never had. `reproduction_gate` fed `investigate`
    unconditionally, so a repository whose tests could not run still went
    through investigation, blast analysis, context engineering, planning, patch
    generation and the whole retry loop — on empty input, spending the two most
    expensive LLM calls in the system to generate nothing.

    Only a *blocking* verdict stops the run. A probe that could not reach a
    conclusion (it errored, or the precheck is disabled) leaves `environment`
    unset and the pipeline behaves exactly as it did before — a diagnostic must
    not become a new failure mode.
    """
    environment = state.get("environment") or {}
    if environment.get("blocking"):
        return "halt_environment"
    return "reproduction_gate"
