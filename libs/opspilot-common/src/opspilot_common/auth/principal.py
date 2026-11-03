"""The signed-in person as every service sees it. Pure data: safe to import from domain code."""

from __future__ import annotations

from dataclasses import dataclass, field

from .roles import Role


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    name: str
    roles: frozenset[Role] = field(default_factory=frozenset)
    mfa: bool = False

    def has_any(self, *roles: Role) -> bool:
        return bool(self.roles & set(roles))

    @property
    def primary_role(self) -> Role | None:
        return next(iter(sorted(self.roles)), None)
