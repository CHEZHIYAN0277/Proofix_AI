from typing import Literal

from pydantic import BaseModel, Field

EvidenceSource = Literal["stack_trace", "finding", "cve", "runtime", "citation"]


class Citation(BaseModel):
    file: str
    line: int
    claim: str
    verified: bool = False


class EvidenceReference(BaseModel):
    source: EvidenceSource
    ref_id: str = ""
    file: str | None = None
    line: int | None = None
    claim: str = ""
    weight: float = 0.0


class RootCauseBrief(BaseModel):
    summary: str = ""
    root_cause: str = ""
    citations: list[Citation] = Field(default_factory=list)
    stack_evidence: str = ""
    affected_modules: list[str] = Field(default_factory=list)
    reinvestigation_required: bool = False
    reinvestigation_count: int = 0
    evidence_incomplete: bool = False
    confidence: float = 0.0
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    runtime_evidence: dict = Field(default_factory=dict)
    cve_context: list[str] = Field(default_factory=list)
