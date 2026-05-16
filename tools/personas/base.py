"""Persona dataclass + allocation result for the scientific-investigation tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Roles are the four canonical positions from Plan §3.
PersonaRole = Literal["author", "devils_advocate", "methodiker", "engineering_reviewer"]

# Provider preferences:
#   "primary"      — same provider as the run's main provider
#   "cross"        — must be a DIFFERENT provider (Devil's Advocate, Engineering-Reviewer)
#   "any_external" — any provider that is NOT the primary; falls back to primary
#                    if cross-provider routing was rejected (#cross-provider:none)
ProviderPreference = Literal["primary", "cross", "any_external"]


@dataclass(frozen=True)
class Persona:
    role: PersonaRole
    name: str
    short_label: str
    provider_preference: ProviderPreference
    system_prompt: str
    description: str

    def as_audit_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "name": self.name,
            "provider_preference": self.provider_preference,
        }


@dataclass(frozen=True)
class PersonaAllocation:
    """One persona resolved to a concrete provider.

    ``provider_name`` is the name of the provider chosen by the allocator.
    ``cross_provider_satisfied`` is True iff the chosen provider is actually
    different from the primary (used by the status-tuple computation in
    later increments).
    """
    persona: Persona
    provider_name: str
    cross_provider_satisfied: bool

    def as_audit_dict(self) -> dict[str, object]:
        return {
            "role": self.persona.role,
            "name": self.persona.name,
            "provider": self.provider_name,
            "cross_provider_satisfied": self.cross_provider_satisfied,
        }
