"""Roles and which roles may decide each approval gate (single source of truth)."""

from enum import StrEnum

from ..events.catalogue import Gate


class Role(StrEnum):
    L1_ANALYST = "l1_analyst"
    SUPPORT_AGENT = "support_agent"
    SUPPORT_MANAGER = "support_manager"
    COMPLIANCE_LEAD = "compliance_lead"
    ADMIN = "admin"


ROLE_LABELS: dict[Role, str] = {
    Role.L1_ANALYST: "L1 Analyst",
    Role.SUPPORT_AGENT: "Support agent",
    Role.SUPPORT_MANAGER: "Support manager",
    Role.COMPLIANCE_LEAD: "Compliance lead",
    Role.ADMIN: "Admin",
}

# Wireframe: T1 ticket → Analyst; T2 production fix → named Support agent only; T3 message → Analyst.
GATE_APPROVERS: dict[Gate, frozenset[Role]] = {
    Gate.TICKET: frozenset({Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER}),
    Gate.FIX: frozenset({Role.SUPPORT_AGENT}),
    Gate.MESSAGE: frozenset({Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER}),
}


def can_decide(roles: frozenset[Role] | set[Role], gate: Gate) -> bool:
    return bool(set(roles) & GATE_APPROVERS[gate])
