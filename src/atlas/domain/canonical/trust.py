"""Trust function v1 (``trust-1``): atlas-specs ``contracts/trust-function.md`` (owner decisions U-3 and G2 closure).

Pure and deterministic. ``at`` is the publishing change set's server time, never the wall clock. No precedence
rule exists (DEC-034 Proposed) and no production freshness policy (DEC-072 Open): :data:`NO_POLICY` keeps both
explicit and conservative. The atlas-specs truth table (``acceptance/test-data/trust-function-v1/cases.json``) is
the acceptance oracle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

FUNCTION_VERSION = "trust-1"
EVIDENCE_KINDS = frozenset({"provider_configuration", "contract", "documentation", "human_assertion", "inferred"})
HUMAN_ACTOR_NAMESPACES = frozenset({"oidc"})
VERIFICATION_SUPPORT = {
    True: frozenset({"provider_configuration", "human_assertion"}),  # physical claims
    False: frozenset({"human_assertion", "documentation", "contract"}),  # logical claims
}
VERIFIABLE_STATES = frozenset({"discovered", "curated", "documented"})
NO_POLICY: dict[str, Any] = {"freshness": [], "precedence": []}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class Claim:
    """``physical``: a physical entity, or a relationship with a physical endpoint. ``allowed_kinds``: relationships."""

    fact_kind: str
    physical: bool
    allowed_kinds: frozenset[str] | None = None

    def applicable_kinds(self) -> frozenset[str]:
        """TF-A. Provider configuration never establishes a logical or business fact on its own."""
        kinds = frozenset(self.allowed_kinds or ()) if self.fact_kind == "relationship" else EVIDENCE_KINDS
        return kinds if self.physical else kinds - {"provider_configuration"}


@dataclass
class TrustResult:
    trust_state: str | None
    verification_state: str
    rule: str
    selected: dict[str, Any] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    verification_reasons: list[str] = field(default_factory=list)


def freshness_of(assertion: dict[str, Any], policy: dict[str, Any], at: str) -> str:
    """TF-F: ``fresh``, ``expired`` or ``unassessed`` (no policy for the evidence's method)."""
    rule = next((p for p in policy.get("freshness", []) if p["method"] == assertion["method"]), None)
    if rule is None:
        return "unassessed"
    age_days = (timestamp(at) - timestamp(assertion["observed_at"])) / 86400
    return "expired" if age_days > rule["stale_after_days"] else "fresh"


def evaluate(
    claim: Claim,
    assertions: list[dict[str, Any]],
    verification: dict[str, Any] | None,
    policy: dict[str, Any],
    at: str,
    prior_values: dict[str, Any] | None = None,
    origin: str = "published",
) -> TrustResult:
    """Evaluate one fact. Assertions carry ``evidence_kind``, ``field_path``, ``asserted_value`` and their evidence's
    ``method``, ``observed_at`` and ``valid_to``."""
    if policy.get("precedence"):
        raise ValueError("trust-1 accepts no precedence rules: DEC-034 values are not approved")
    if origin == "draft":
        return TrustResult("proposed", "unverified", "TF-0")
    current = [a for a in assertions if not a.get("valid_to") or timestamp(a["valid_to"]) > timestamp(at)]
    existence = [a for a in current if a["field_path"] is None and a["evidence_kind"] in claim.applicable_kinds()]
    values: dict[str, set[str]] = {}
    for a in current:
        if a["field_path"] is not None:
            values.setdefault(a["field_path"], set()).add(canonical_json(a["asserted_value"]))
    conflicts = sorted(p for p, vs in values.items() if len(vs) > 1)
    selected = {
        p: (prior_values or {}).get(p) if p in conflicts else json.loads(next(iter(vs))) for p, vs in values.items()
    }
    if not existence:
        return TrustResult(None, "unverified", "TF-1", selected, conflicts)
    usable = [a for a in existence if freshness_of(a, policy, at) != "expired"]
    kinds = {a["evidence_kind"] for a in usable}
    v = verification or {}
    recorded = v.get("verification_state") == "verified"
    expired = recorded and v.get("expires_at") is not None and timestamp(v["expires_at"]) <= timestamp(at)
    if conflicts:
        state, rule = "conflicted", "TF-2"
    elif expired:
        state, rule = "stale", "TF-3a"
    elif not usable:
        state, rule = "stale", "TF-3b"
    elif "provider_configuration" in kinds:
        state, rule = "discovered", "TF-4"
    elif "human_assertion" in kinds:
        state, rule = "curated", "TF-5"
    elif kinds & {"documentation", "contract"}:
        state, rule = "documented", "TF-6"
    else:
        state, rule = "inferred", "TF-7"
    reasons: list[str] = []
    if recorded:
        if (v.get("verified_by") or {}).get("namespace") not in HUMAN_ACTOR_NAMESPACES:
            reasons.append("verified_by is not a human actor")
        if not v.get("verified_at") or timestamp(v["verified_at"]) > timestamp(at):
            reasons.append("verified_at missing or after the evaluation time")
        if state not in VERIFIABLE_STATES:
            reasons.append(f"trust state {state}")
        support = VERIFICATION_SUPPORT[claim.physical]
        if not kinds & support:
            reasons.append(f"no current evidence of {sorted(support)}")
    return TrustResult(
        state, "verified" if recorded and not reasons else "unverified", rule, selected, conflicts, reasons
    )
