from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from compliance.util import normalise_text

# Deliberately conservative: a legal question may legitimately contain words
# such as "termination" or "harassment".  These patterns target instructions to
# the model, not subject matter.
INJECTION_PATTERNS: list[tuple[str, str, str]] = [
    (r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)", "ignore_previous", "high"),
    (r"\bdisregard\s+(the\s+)?(system|developer|previous|above)\s+(prompt|instructions?|message)", "disregard_instructions", "high"),
    (r"\b(reveal|show|print|repeat|output)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions?|rules?)", "prompt_exfiltration", "high"),
    (r"\byou\s+are\s+now\s+(a|an|the)?\s*\w+", "role_override", "high"),
    (r"\b(act|behave|respond)\s+as\s+(if\s+you\s+are\s+)?(a|an|the)\s+", "role_override", "medium"),
    (r"\b(jailbreak|developer\s+mode|dan\s+mode|do\s+anything\s+now)\b", "jailbreak", "high"),
    (r"\b(call|invoke|use)\s+(the\s+)?(tool|function|api)\s+", "tool_directive", "medium"),
    (r"\b(delete|drop|truncate|overwrite|wipe)\s+(the\s+)?(index|database|corpus|files?|data)", "destructive_directive", "high"),
    (r"\b(exfiltrate|send|post|upload)\s+(the\s+)?(data|corpus|secrets?|keys?|prompt)\b", "exfiltration", "high"),
    (r"\b(pretend|imagine)\s+(that\s+)?you\s+(are|have)\b", "role_override", "medium"),
    (r"\boverride\s+(the\s+)?(safety|guardrail|policy|instructions?)", "override_policy", "high"),
    (r"<\|?\s*(system|assistant|developer)\s*\|?>", "role_token", "high"),
    (r"\[\s*(system|assistant|developer)\s*\]", "role_token", "medium"),
]

TOXIC_PATTERNS: list[tuple[str, str, str]] = [
    (r"\b(kill|murder|hurt|harm)\s+(yourself|himself|herself|them|someone)\b", "threat", "high"),
    (r"\b(i\s+will|i'll|we\s+will|we'll)\s+(kill|hurt|attack|destroy)\s+", "threat", "high"),
    (r"\b(kill|hurt)\s+you\b", "threat", "high"),
    (r"\b(bitch|bastard|slut|whore|retard|faggot|nigger|chink|kike)\b", "slur", "high"),
    (r"\b(shut\s+up|screw\s+you|f\*+k\s+you|fuck\s+you)\b", "abuse", "medium"),
]

UNAUTHORISED_DETERMINATION_PATTERNS: list[tuple[str, str]] = [
    (r"\bwe\s+(hereby\s+)?(determine|decide|conclude|find)\s+that\b", "we determine"),
    (r"\bthis\s+is\s+(our\s+)?(final|binding)\s+(decision|determination|advice)\b", "final decision"),
    (r"\byou\s+(will|are\s+guaranteed\s+to)\s+win\b", "prediction of outcome"),
    (r"\bthe\s+court\s+will\s+(hold|find|decide)\b", "prediction of court outcome"),
    (r"\bwe\s+(advise|recommend)\s+you\s+to\s+(sue|litigate|file\s+a\s+case)\b", "legal advice"),
    (r"\blegally\s+(liable|guilty)\b", "legal conclusion"),
    (r"\bwe\s+are\s+your\s+(lawyer|attorney|counsel)\b", "lawyer claim"),
    (r"\bguarantee(d)?\s+(outcome|result|compensation)\b", "guarantee"),
]


@dataclass
class GuardrailFinding:
    kind: str
    severity: str
    match: str
    pattern: str


@dataclass
class GuardrailResult:
    allowed: bool
    sanitised: str
    findings: list[GuardrailFinding] = field(default_factory=list)
    action: str = "none"

    @property
    def blocked(self) -> bool:
        return self.action == "quarantine"

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "findings": [f.__dict__ for f in self.findings],
        }


def _scan(texts: Iterable[tuple[str, str]]) -> list[GuardrailFinding]:
    findings: list[GuardrailFinding] = []
    for field_name, text in texts:
        if not text:
            continue
        normalised = normalise_text(text)
        for pattern, label, severity in INJECTION_PATTERNS:
            for match in re.finditer(pattern, normalised, flags=re.IGNORECASE):
                findings.append(
                    GuardrailFinding(
                        kind="prompt_injection",
                        severity=severity,
                        match=f"{field_name}: {match.group(0)[:120]}",
                        pattern=label,
                    )
                )
        for pattern, label, severity in TOXIC_PATTERNS:
            for match in re.finditer(pattern, normalised, flags=re.IGNORECASE):
                findings.append(
                    GuardrailFinding(
                        kind="toxicity",
                        severity=severity,
                        match=f"{field_name}: {match.group(0)[:120]}",
                        pattern=label,
                    )
                )
    return findings


def _neutralise(text: str) -> str:
    cleaned = text.replace("<", "‹").replace(">", "›")
    for pattern, _label, _severity in INJECTION_PATTERNS:
        cleaned = re.sub(pattern, "[removed-instruction]", cleaned, flags=re.IGNORECASE)
    return cleaned


def check_free_text(
    texts: dict[str, str],
    *,
    injection_mode: str = "quarantine",
    toxicity_mode: str = "quarantine",
) -> GuardrailResult:
    findings = _scan((k, v) for k, v in texts.items() if isinstance(v, str))
    if not findings:
        return GuardrailResult(allowed=True, sanitised=json_safe_inputs(texts))
    has_high = any(f.severity == "high" for f in findings)
    action = "none"
    if has_high:
        injection_high = any(f.kind == "prompt_injection" and f.severity == "high" for f in findings)
        toxicity_high = any(f.kind == "toxicity" and f.severity == "high" for f in findings)
        if (injection_high and injection_mode == "quarantine") or (
            toxicity_high and toxicity_mode == "quarantine"
        ):
            action = "quarantine"
        else:
            action = "sanitise"
    elif findings:
        action = "sanitise"
    sanitised = json_safe_inputs(texts) if action != "sanitise" else _neutralise_inputs(texts)
    return GuardrailResult(allowed=action != "quarantine", sanitised=sanitised, findings=findings, action=action)


def _neutralise_inputs(texts: dict[str, str]) -> str:
    return "\n".join(f"{key}: {_neutralise(value)}" for key, value in texts.items() if value)


def sanitise_mapping(texts: dict[str, str]) -> dict[str, str]:
    """Return the same fields with instruction-like spans neutralised."""
    return {key: _neutralise(value) for key, value in texts.items()}


def json_safe_inputs(texts: dict[str, str]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in texts.items() if value)


def filter_output(text: str) -> tuple[str, list[str]]:
    """Remove statements the desk is not authorised to make.

    Returns (filtered_text, blocked_phrases).  The advisory may still state what
    a provision says; it may not issue a legal opinion or predict an outcome.
    """
    blocked: list[str] = []
    filtered = text
    for pattern, label in UNAUTHORISED_DETERMINATION_PATTERNS:
        if re.search(pattern, filtered, flags=re.IGNORECASE):
            blocked.append(label)
            filtered = re.sub(
                pattern,
                "[removed: not authorised to issue a legal determination]",
                filtered,
                flags=re.IGNORECASE,
            )
    return filtered, blocked


def guardrail_disclaimer() -> str:
    return (
        "This is a cited compliance advisory, not legal advice or a legal opinion. "
        "A qualified professional must decide the matter."
    )
