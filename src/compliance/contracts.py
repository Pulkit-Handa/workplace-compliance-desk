from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Domain(str, Enum):
    GRATUITY = "gratuity"
    MATERNITY_BENEFIT = "maternity_benefit"
    WORKPLACE_HARASSMENT = "workplace_harassment"
    OTHER = "other"


class Route(str, Enum):
    ANSWER_WITH_CITATION = "answer_with_citation"
    REQUEST_MORE_ESTABLISHMENT_DETAIL = "request_more_establishment_detail"
    LEGAL_COUNSEL_REVIEW = "legal_counsel_review"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class EmploymentType(str, Enum):
    PERMANENT = "permanent"
    FIXED_TERM = "fixed_term"
    CONTRACT = "contract"
    CONSULTANT = "consultant"
    APPRENTICE = "apprentice"
    UNKNOWN = "unknown"


class SeparationEvent(str, Enum):
    NONE = "none"
    RESIGNATION = "resignation"
    RETIREMENT = "retirement"
    SUPERANNUATION = "superannuation"
    EXPIRY = "expiry"
    TERMINATION = "termination"
    DEATH = "death"
    DISABLEMENT = "disablement"
    UNKNOWN = "unknown"


class EstablishmentType(str, Enum):
    FACTORY = "factory"
    SHOP = "shop"
    IT_SERVICES = "it_services"
    HOSPITAL = "hospital"
    EDUCATIONAL = "educational"
    NGO = "ngo"
    GOVERNMENT = "government"
    OTHER = "other"


class RequesterRole(str, Enum):
    HR = "hr"
    EMPLOYEE = "employee"
    MANAGER = "manager"
    OTHER = "other"


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


class Establishment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    establishment_type: EstablishmentType = EstablishmentType.OTHER
    employee_count: int | None = Field(default=None, ge=1, le=1_000_000)
    state: str | None = Field(default=None, max_length=60)
    has_internal_committee: bool | None = None


class Employment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_type: EmploymentType | None = None
    start_date: date | None = None
    end_date: date | None = None
    event: SeparationEvent = SeparationEvent.NONE
    completed_service_months: int | None = Field(default=None, ge=0, le=720)
    monthly_wage_inr: float | None = Field(default=None, ge=0, le=10_000_000)
    is_pregnant: bool | None = None
    expected_delivery_date: date | None = None
    service_days_last_12_months: int | None = Field(default=None, ge=0, le=366)
    children_count: int | None = Field(default=None, ge=0, le=20)


class Requester(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    role: RequesterRole = RequesterRole.HR
    organisation: str | None = Field(default=None, max_length=120)


class ComplianceRequest(BaseModel):
    """Published request contract for POST /v1/advisories.

    The contract is deliberately stricter than a free-form chat prompt.  It is
    the only place at which a run can begin; validation happens before the queue.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=10, max_length=2000)
    establishment: Establishment
    employment: Employment
    requester: Requester
    as_of_date: date = Field(default_factory=date.today)
    consent: bool = Field(description="Caller attests the request contains synthetic data only")
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def cross_field_checks(self) -> ComplianceRequest:
        emp = self.employment
        errors: list[str] = []
        if not self.consent:
            errors.append("consent must be true: only synthetic data may enter the system")
        if emp.start_date and emp.end_date and emp.end_date <= emp.start_date:
            errors.append("employment.end_date must be after employment.start_date")
        if emp.start_date and emp.start_date > self.as_of_date:
            errors.append("employment.start_date cannot be after as_of_date")
        if emp.end_date and emp.end_date > self.as_of_date:
            errors.append("employment.end_date cannot be after as_of_date")
        if emp.event == SeparationEvent.EXPIRY and emp.worker_type not in (
            EmploymentType.FIXED_TERM,
            EmploymentType.CONTRACT,
            EmploymentType.UNKNOWN,
            None,
        ):
            errors.append("employment.event=expiry requires worker_type fixed_term or contract")
        if emp.worker_type == EmploymentType.FIXED_TERM and emp.end_date and not emp.start_date:
            errors.append("fixed_term employment with end_date also requires start_date")
        if emp.start_date and emp.end_date:
            months = round((emp.end_date - emp.start_date).days / 30.4375)
            if emp.completed_service_months is not None and abs(months - emp.completed_service_months) > 1:
                errors.append(
                    "employment.completed_service_months is inconsistent with start_date and end_date "
                    f"(dates imply about {months} months)"
                )
        if emp.is_pregnant is False and emp.expected_delivery_date is not None:
            errors.append("expected_delivery_date cannot be supplied when is_pregnant is false")
        if emp.expected_delivery_date and emp.expected_delivery_date > self.as_of_date:
            delta = (emp.expected_delivery_date - self.as_of_date).days
            if delta > 300:
                errors.append("expected_delivery_date cannot be more than 300 days after as_of_date")
        if emp.is_pregnant and emp.expected_delivery_date is None and emp.service_days_last_12_months is None:
            # Not an error: the advisory can ask for the missing fact.  Kept here
            # as a documented cross-field relationship.
            pass
        if errors:
            raise ValueError("; ".join(errors))
        return self


class Citation(BaseModel):
    id: str
    citation: str
    statute: str
    section: str
    clause: str | None = None
    heading: str | None = None
    status: str
    effective_from: date | None = None
    effective_to: date | None = None
    repealed_by: str | None = None
    quote: str
    source_url: str
    retrieved_rank: int | None = None
    support_score: float = 0.0


class Claim(BaseModel):
    id: str
    text: str
    citations: list[str] = Field(default_factory=list)
    kind: Literal["operative", "background", "procedural", "escalation"] = "operative"
    supported: bool = False
    support_score: float = 0.0
    reason: str | None = None


class VerificationIssue(BaseModel):
    claim_id: str
    reason: str
    detail: str
    suggested_query: str | None = None


class VerificationResult(BaseModel):
    outcome: VerificationOutcome
    claims_checked: int = 0
    claims_supported: int = 0
    issues: list[VerificationIssue] = Field(default_factory=list)
    loops: int = 0
    checks: list[str] = Field(default_factory=list)
    notes: str | None = None


class RouteDecision(BaseModel):
    route: Route
    basis: str
    confidence: float = Field(ge=0.0, le=1.0)
    escalation_path: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class UsageStep(BaseModel):
    step: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""
    prompt_version: str = ""


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    per_step: list[UsageStep] = Field(default_factory=list)


class GuardrailReport(BaseModel):
    input_action: str = "none"
    findings: list[dict[str, Any]] = Field(default_factory=list)
    output_filtered_phrases: list[str] = Field(default_factory=list)
    toxicity_action: str = "none"


class AdvisoryResult(BaseModel):
    run_id: str
    correlation_id: str
    status: RunStatus
    route: Route
    domain: Domain
    advisory: str
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    verification: VerificationResult
    route_decision: RouteDecision
    guardrails: GuardrailReport = Field(default_factory=GuardrailReport)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    degraded: bool = False
    cached: bool = False
    index_version: str | None = None
    created_at: str
    completed_at: str
    disclaimer: str = (
        "This is a cited compliance advisory, not legal advice or a legal opinion. "
        "A qualified professional must decide the matter."
    )


class QuarantineResponse(BaseModel):
    error: str = "validation_failed"
    correlation_id: str
    quarantine_id: str
    field_errors: list[dict[str, Any]]
    agent_started: bool = False
    message: str = "Request rejected at the ingestion boundary. No agent run was started."


class AcceptedRun(BaseModel):
    run_id: str
    correlation_id: str
    status: RunStatus
    poll_url: str
    estimated_seconds: int = 15
    cached: bool = False


class RunEnvelope(BaseModel):
    run_id: str
    correlation_id: str
    status: RunStatus
    progress: str | None = None
    result: AdvisoryResult | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)
    category: Literal["accuracy", "citation", "missing_context", "escalation", "latency", "other"] = "other"


class FeedbackResponse(BaseModel):
    feedback_id: str
    run_id: str
    recorded: bool = True


class FeedbackSummary(BaseModel):
    total: int = 0
    satisfactory: int = 0
    unsatisfactory: int = 0
    satisfaction_rate: float = 0.0
    by_category: dict[str, int] = Field(default_factory=dict)
    recent: list[dict[str, Any]] = Field(default_factory=list)


class TraceEvent(BaseModel):
    seq: int
    ts: str
    elapsed_ms: float
    kind: str
    agent: str | None = None
    name: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunTrace(BaseModel):
    run_id: str
    correlation_id: str
    request: dict[str, Any] | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    result: AdvisoryResult | None = None


__all__ = [
    "AdvisoryResult",
    "AcceptedRun",
    "Citation",
    "Claim",
    "ComplianceRequest",
    "Domain",
    "Employment",
    "EmploymentType",
    "Establishment",
    "EstablishmentType",
    "FeedbackRequest",
    "FeedbackResponse",
    "FeedbackSummary",
    "GuardrailReport",
    "QuarantineResponse",
    "Requester",
    "RequesterRole",
    "Route",
    "RouteDecision",
    "RunEnvelope",
    "RunStatus",
    "RunTrace",
    "SeparationEvent",
    "TraceEvent",
    "Usage",
    "UsageStep",
    "VerificationIssue",
    "VerificationOutcome",
    "VerificationResult",
]
