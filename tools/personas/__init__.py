"""Persona definitions for the scientific-investigation multi-persona review.

Plan §3: four personas total — Author (primary investigator), Devil's
Advocate (adversarial), Methodiker (methodology critic), and Engineering-
Reviewer (Phase 7 cross-provider). Engineering-Reviewer is built later
(I7) because it sits outside the Phase-2 multi-persona review.

This module only defines static persona metadata + system prompts. The
allocator (``phase_persona_allocation``) decides which provider runs which
persona; the actual LLM invocation lives in Phase 2 (I3).
"""

from tools.personas.base import Persona, PersonaAllocation
from tools.personas.author import AUTHOR
from tools.personas.devils_advocate import DEVILS_ADVOCATE
from tools.personas.methodiker import METHODIKER

__all__ = [
    "Persona",
    "PersonaAllocation",
    "AUTHOR",
    "DEVILS_ADVOCATE",
    "METHODIKER",
    "ALL_PHASE2_PERSONAS",
]

# Order is meaningful: Author writes first, DA challenges, Methodiker reviews
# methodology last. Phase 2 (I3) iterates this list in order.
ALL_PHASE2_PERSONAS: tuple[Persona, ...] = (AUTHOR, DEVILS_ADVOCATE, METHODIKER)
