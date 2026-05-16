"""Phase implementations for the BrainstormTool.

Phases
------
0   — Topic Analysis & Persona Generation (LLM picks 4-6 domain-aware personas).
0.5 — Provider Allocation (round-robin across providers if #cross-provider tag set).
1   — Initial Idea Generation (each persona produces ideas independently).
2   — Cross-Pollination (each persona sees peers' ideas, contributes new ones).
K   — Convergence Check (TF-IDF cluster growth ratio; stop when below threshold).
3   — Synthesis & Ranking (LLM picks Top-N from clusters with Pro/Contra).

Each phase is a pure function that takes a provider + state, returns ideas
plus the raw RunResult so the caller can aggregate tokens / handle errors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from providers.base import BaseProvider
from tools.crosschecks.similarity_index import cosine_similarity
from tools.scientific_investigation_phases import (
    _extract_yaml_block,
    _parse_yaml_minimal,
)

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrainstormPersona:
    key: str
    name: str
    role_description: str
    perspective_focus: str
    system_prompt: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "name": self.name,
            "role_description": self.role_description,
            "perspective_focus": self.perspective_focus,
            "system_prompt": self.system_prompt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "BrainstormPersona":
        return cls(
            key=str(d["key"]),
            name=str(d["name"]),
            role_description=str(d.get("role_description", "")),
            perspective_focus=str(d.get("perspective_focus", "")),
            system_prompt=str(d["system_prompt"]),
        )


@dataclass(frozen=True)
class BrainstormAllocation:
    persona: BrainstormPersona
    provider_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona.as_dict(),
            "provider_name": self.provider_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrainstormAllocation":
        return cls(
            persona=BrainstormPersona.from_dict(d["persona"]),
            provider_name=str(d["provider_name"]),
        )


@dataclass
class BrainstormIdea:
    text: str
    persona_key: str
    iteration: int
    cluster_id: int = -1  # -1 = unassigned

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "persona_key": self.persona_key,
            "iteration": self.iteration,
            "cluster_id": self.cluster_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrainstormIdea":
        return cls(
            text=str(d["text"]),
            persona_key=str(d["persona_key"]),
            iteration=int(d.get("iteration", 1)),
            cluster_id=int(d.get("cluster_id", -1)),
        )


@dataclass
class ConvergenceResult:
    cluster_count_before: int
    cluster_count_after: int
    new_clusters: int
    new_cluster_ratio: float  # 0..1
    converged: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_count_before": self.cluster_count_before,
            "cluster_count_after": self.cluster_count_after,
            "new_clusters": self.new_clusters,
            "new_cluster_ratio": round(self.new_cluster_ratio, 4),
            "converged": self.converged,
        }


# ── Prompts (use .replace, not .format — user topic may contain braces) ───


_PERSONA_PROMPT = """\
Du leitest eine Brainstorming-Session zu folgendem Thema:

THEMA: {topic}

Waehle {min_personas}-{max_personas} DIVERSE Personas, die die ideale Round-Table-Besetzung fuer dieses Thema bilden. \
Vermeide generische Rollen — waehle Personas, die im konkreten Themenbereich SPEZIFISCHE, KONTRASTIVE Perspektiven liefern.

Beispiele guter Persona-Sets (themenabhaengig):
  - Pricing: Discount-Kaeufer, Premium-Kaeufer, Daten-Analyst, Mitbewerber
  - API-Design: Frontend-Dev, Performance-Engineer, Junior-Konsument, Security-Auditor
  - Marketing: Markenchef, Zielgruppen-Vertreter, Konkurrent, Conversion-Analyst

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```), ohne erklaerenden Text. Schema:

```yaml
personas:
  - key: <kebab-case-slug, nur a-z0-9->
    name: <Persona-Name, max 50 chars>
    role_description: <Eine Zeile: Wer ist diese Person?>
    perspective_focus: <Eine Zeile: Welchen unique Blickwinkel bringt sie ein?>
    system_prompt: "<Mind. 3 Saetze in EINER Zeile. Hintergrund, Werte, typische Denkmuster, Vokabular. Konkrete Anweisung wie diese Persona ein Brainstorming angeht.>"
```

WICHTIG:
- Alle keys MUESSEN unique sein (kebab-case, nur a-z0-9-).
- Alle Personas MUESSEN merklich unterschiedliche Perspektiven haben.
- system_prompt MUSS in EINER Zeile als quoted string stehen (keine `|` Block-Skalare) \
  und mindestens 100 Zeichen lang sein.
- Genau {min_personas}-{max_personas} Personas, keine mehr, keine weniger.
"""


_INITIAL_GEN_PROMPT = """\
{persona_prompt}

---

## Brainstorming-Auftrag

**Thema:** {topic}

Produziere bis zu {max_ideas} Ideen aus DEINER Perspektive. Schreibe jede Idee in 1-2 Saetzen.

**Output-Format** (genau so):

```ideas
1. <Eine konkrete Idee, 1-2 Saetze>
2. <Eine andere Idee, 1-2 Saetze>
...
```

**Regeln:**
- Quantitaet vor Qualitaet — auch unkonventionelle Ideen einschliessen.
- KEINE Meta-Kommentare ("Als <Persona> wuerde ich..."), nur die Ideen.
- Jede Idee muss aus DEINER spezifischen Perspektive stammen, nicht allgemein.
- Halte dich an die Output-Form mit Nummerierung 1., 2., ... innerhalb des ```ideas-Blocks.
"""


_CROSS_POLLINATION_PROMPT = """\
{persona_prompt}

---

## Round-Table — Iteration {iteration}

**Thema:** {topic}

In der vorherigen Runde haben die MitstreiterInnen folgende Ideen eingebracht:

{peer_ideas_block}

Deine bisherigen Ideen (zur Erinnerung — NICHT wiederholen):

{own_ideas_block}

**Deine Aufgabe** — produziere bis zu {max_ideas} NEUE Beitraege nach folgenden Kategorien (verteile dich frei, nicht alle Kategorien noetig):

1. **Aufbau-Ideen**: Ideen die auf Peer-Ideen aufbauen / sie konkretisieren.
2. **Synthese-Ideen**: Ideen die zwei oder mehr Peer-Ideen kombinieren.
3. **Challenge-Ideen**: Aspekte die Peer-Ideen ignorieren — formulier daraus eine eigene Idee.
4. **Gap-Ideen**: Was haben alle bisher uebersehen? Aus deiner Perspektive.

**Output-Format** (genau so):

```ideas
1. <Eine konkrete neue Idee, 1-2 Saetze>
2. <Eine andere neue Idee, 1-2 Saetze>
...
```

**Regeln:**
- KEINE der eigenen frueheren Ideen wiederholen.
- Jede neue Idee muss aus DEINER Perspektive sinnvoll sein.
- Bleib im ```ideas-Block mit Nummerierung.
"""


_SYNTHESIS_PROMPT = """\
Du bist Synthesizer fuer eine Brainstorming-Session.

**Thema:** {topic}

**Personas, die teilgenommen haben:**
{personas_block}

**Alle Ideen ({total_ideas} insgesamt, schon vorgeclusterte):**

{clusters_block}

**Deine Aufgabe:**
Waehle die Top-{top_n} Ideen aus allen Clustern aus. Beruecksichtige dabei:
- Konkrete Umsetzbarkeit
- Originalitaet
- Cluster-Groesse (eine Idee, die mehrfach unabhaengig auftauchte, ist wertvoll; \
auch Einzel-Ideen koennen gewinnen, wenn sie hervorragend sind)
- Diversitaet der Top-N (nicht 5 sehr aehnliche Top-Ideen)

**Output (Markdown), genau so beginnen:**

## Top-{top_n} Ideen

### 1. <Idee-Titel, 5-10 Woerter>
- **Ursprung:** Cluster #X (Personas: <key1>, <key2>)
- **Kern-Idee:** <Genaue Formulierung, 2-3 Saetze>
- **Pro:** <Was spricht dafuer, 1-2 Saetze>
- **Contra:** <Was spricht dagegen / Risiken, 1-2 Saetze>
- **Naechster Schritt:** <Eine konkrete Aktion, um es zu testen, 1 Satz>

### 2. ...
(weiter bis #{top_n})

Keine Boilerplate, keine Meta-Kommentare. Direkt mit "## Top-{top_n} Ideen" starten.
"""


# ── Phase 0: Topic Analysis & Persona Generation ──────────────────────


def phase_topic_analysis(
    topic: str,
    provider: BaseProvider,
    *,
    min_personas: int,
    max_personas: int,
    timeout_sec: int,
    cwd: str | None = None,
) -> tuple[list[BrainstormPersona], Any]:
    """Phase 0: LLM analyses the topic and proposes domain-aware personas.

    Returns ``(personas, raw_result)`` so the caller can aggregate tokens.
    Raises ``RuntimeError`` if the LLM call fails or output is unparseable.
    """
    prompt = (
        _PERSONA_PROMPT
        .replace("{topic}", topic)
        .replace("{min_personas}", str(min_personas))
        .replace("{max_personas}", str(max_personas))
    )
    result = provider.run(prompt, cwd=cwd, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"persona-generation LLM call failed: {getattr(result, 'error', 'unknown')}"
        )
    yaml_text = _extract_yaml_block(result.output or "")
    parsed = _parse_yaml_minimal(yaml_text)
    personas = parse_personas(parsed, min_personas=min_personas, max_personas=max_personas)
    return personas, result


def parse_personas(
    parsed: Any, *, min_personas: int, max_personas: int,
) -> list[BrainstormPersona]:
    """Validate and convert a parsed YAML mapping into BrainstormPersona objects.

    Raises ``ValueError`` on any structural or content violation.
    """
    if not isinstance(parsed, dict) or "personas" not in parsed:
        raise ValueError("persona output missing 'personas' key")
    raw_list = parsed.get("personas")
    if not isinstance(raw_list, list):
        raise ValueError("personas must be a list")
    if not (min_personas <= len(raw_list) <= max_personas):
        raise ValueError(
            f"persona count {len(raw_list)} outside allowed range "
            f"{min_personas}-{max_personas}"
        )
    key_re = re.compile(r"^[a-z0-9-]+$")
    keys_seen: set[str] = set()
    personas: list[BrainstormPersona] = []
    for idx, raw in enumerate(raw_list, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"persona #{idx} not a mapping: {raw!r}")
        key = str(raw.get("key", "")).strip()
        if not key or not key_re.match(key):
            raise ValueError(f"persona #{idx} key {key!r} invalid (must be kebab-case a-z0-9-)")
        if key in keys_seen:
            raise ValueError(f"duplicate persona key: {key}")
        keys_seen.add(key)
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError(f"persona {key} has empty name")
        sys_prompt = str(raw.get("system_prompt", "")).strip()
        if len(sys_prompt) < 100:
            raise ValueError(
                f"persona {key} system_prompt too short "
                f"({len(sys_prompt)} chars, need >=100)"
            )
        personas.append(BrainstormPersona(
            key=key,
            name=name,
            role_description=str(raw.get("role_description", "")).strip(),
            perspective_focus=str(raw.get("perspective_focus", "")).strip(),
            system_prompt=sys_prompt,
        ))
    return personas


# ── Phase 0.5: Provider Allocation ────────────────────────────────────


def phase_provider_allocation(
    personas: list[BrainstormPersona],
    *,
    primary_provider_name: str,
    cross_provider: bool,
    provider_lookup: Callable[[str], Any] | None = None,
    candidate_names: tuple[str, ...] = ("claude", "gemini", "codex", "openrouter"),
) -> list[BrainstormAllocation]:
    """Allocate each persona to a concrete provider.

    Default (``cross_provider=False``): everyone uses the primary provider —
    enables session-cache reuse if extended later.
    With ``cross_provider=True``: round-robin across primary + every other
    provider that ``provider_lookup`` can resolve. Falls back gracefully to
    primary-only when no cross providers are available.
    """
    if not cross_provider:
        return [
            BrainstormAllocation(persona=p, provider_name=primary_provider_name)
            for p in personas
        ]
    if provider_lookup is None:
        from dispatcher import get_provider_by_name as _lookup
        provider_lookup = _lookup
    available: list[str] = [primary_provider_name]
    for name in candidate_names:
        if name == primary_provider_name:
            continue
        if provider_lookup(name) is not None:
            available.append(name)
    return [
        BrainstormAllocation(
            persona=personas[idx],
            provider_name=available[idx % len(available)],
        )
        for idx in range(len(personas))
    ]


# ── Phase 1 & 2: Idea Generation / Cross-Pollination ──────────────────


_IDEAS_BLOCK_RE = re.compile(r"```ideas\s*\n(.*?)\n```", re.DOTALL)
_IDEA_LINE_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")


def parse_ideas(text: str, *, max_ideas: int) -> list[str]:
    """Extract numbered idea lines from a ```ideas block.

    Falls back to a free-form numbered list when the fence is missing.
    Truncated to ``max_ideas`` items. Empty input returns an empty list.
    """
    if not text:
        return []
    match = _IDEAS_BLOCK_RE.search(text)
    body = match.group(1) if match else text
    ideas: list[str] = []
    for line in body.splitlines():
        m = _IDEA_LINE_RE.match(line)
        if m:
            idea = m.group(1).strip()
            if idea:
                ideas.append(idea)
                if len(ideas) >= max_ideas:
                    break
    return ideas


def phase_idea_generation(
    persona: BrainstormPersona,
    provider: BaseProvider,
    *,
    topic: str,
    max_ideas: int,
    timeout_sec: int,
    cwd: str | None = None,
) -> tuple[list[str], Any]:
    """Phase 1: persona produces initial ideas independently.

    Returns ``(ideas, raw_result)``. On error, ``ideas`` is an empty list and
    the caller inspects ``raw_result`` for ``success`` / ``error``.
    """
    prompt = (
        _INITIAL_GEN_PROMPT
        .replace("{persona_prompt}", persona.system_prompt)
        .replace("{topic}", topic)
        .replace("{max_ideas}", str(max_ideas))
    )
    result = provider.run(prompt, cwd=cwd, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        return [], result
    ideas = parse_ideas(result.output or "", max_ideas=max_ideas)
    return ideas, result


def phase_cross_pollination(
    persona: BrainstormPersona,
    provider: BaseProvider,
    *,
    topic: str,
    iteration: int,
    own_ideas: list[BrainstormIdea],
    peer_ideas_by_persona: dict[str, list[BrainstormIdea]],
    max_ideas: int,
    timeout_sec: int,
    max_peer_chars_per_persona: int,
    max_total_inject_chars: int,
    cwd: str | None = None,
) -> tuple[list[str], Any]:
    """Phase 2: persona sees peers' ideas, contributes new ones.

    Truncation: each peer's ideas block capped at ``max_peer_chars_per_persona``;
    the combined block capped at ``max_total_inject_chars``.
    """
    own_block = _format_ideas_block(own_ideas) or "(noch keine Ideen)"
    peer_block = _format_peers_block(
        peer_ideas_by_persona,
        max_chars_per_persona=max_peer_chars_per_persona,
        max_total_chars=max_total_inject_chars,
    ) or "(keine Peer-Ideen verfuegbar)"
    prompt = (
        _CROSS_POLLINATION_PROMPT
        .replace("{persona_prompt}", persona.system_prompt)
        .replace("{topic}", topic)
        .replace("{iteration}", str(iteration))
        .replace("{peer_ideas_block}", peer_block)
        .replace("{own_ideas_block}", own_block)
        .replace("{max_ideas}", str(max_ideas))
    )
    result = provider.run(prompt, cwd=cwd, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        return [], result
    ideas = parse_ideas(result.output or "", max_ideas=max_ideas)
    return ideas, result


def _format_ideas_block(ideas: list[BrainstormIdea]) -> str:
    return "\n".join(f"{i + 1}. {idea.text}" for i, idea in enumerate(ideas))


def _format_peers_block(
    peer_ideas_by_persona: dict[str, list[BrainstormIdea]],
    *,
    max_chars_per_persona: int,
    max_total_chars: int,
) -> str:
    parts: list[str] = []
    total = 0
    for persona_key, ideas in peer_ideas_by_persona.items():
        if not ideas:
            continue
        block = _format_ideas_block(ideas)
        if len(block) > max_chars_per_persona:
            block = block[:max_chars_per_persona] + "\n...[truncated]"
        section = f"### Persona: {persona_key}\n{block}"
        if total + len(section) > max_total_chars:
            break
        parts.append(section)
        total += len(section)
    return "\n\n".join(parts)


# ── Convergence: clustering + check ──────────────────────────────────


def cluster_ideas(
    ideas: list[BrainstormIdea],
    *,
    similarity_threshold: float,
) -> list[list[int]]:
    """Greedy single-pass clustering of ideas.

    Each idea joins the first existing cluster whose representative has
    ``cosine_similarity >= threshold``; otherwise starts a new cluster.
    Side-effect: writes ``cluster_id`` on each idea. Returns clusters as
    lists of idea-indices into the input list.
    """
    clusters: list[list[int]] = []
    representatives: list[str] = []
    for idx, idea in enumerate(ideas):
        matched: int | None = None
        for cid, rep in enumerate(representatives):
            if cosine_similarity(idea.text, rep) >= similarity_threshold:
                matched = cid
                break
        if matched is None:
            matched = len(clusters)
            clusters.append([])
            representatives.append(idea.text)
        clusters[matched].append(idx)
        idea.cluster_id = matched
    return clusters


def check_convergence(
    previous_count: int,
    current_clusters: list[list[int]],
    *,
    threshold: float,
) -> ConvergenceResult:
    """Compute the new-cluster ratio between two consecutive iterations.

    ``converged`` is True iff the ratio is below ``threshold`` AND at least
    one round has produced clusters (otherwise we'd converge on an empty
    state, which is meaningless).
    """
    current = len(current_clusters)
    new = max(0, current - previous_count)
    ratio = (new / current) if current else 0.0
    converged = current > 0 and previous_count > 0 and ratio < threshold
    return ConvergenceResult(
        cluster_count_before=previous_count,
        cluster_count_after=current,
        new_clusters=new,
        new_cluster_ratio=ratio,
        converged=converged,
    )


# ── Phase 3: Synthesis ───────────────────────────────────────────────


def phase_synthesis(
    provider: BaseProvider,
    *,
    topic: str,
    personas: list[BrainstormPersona],
    ideas: list[BrainstormIdea],
    clusters: list[list[int]],
    top_n: int,
    timeout_sec: int,
    cwd: str | None = None,
) -> tuple[str, Any]:
    """Phase 3: Synthesizer picks top-N from clusters; returns markdown text."""
    personas_block = "\n".join(
        f"- **{p.name}** (`{p.key}`): {p.role_description}" for p in personas
    )
    clusters_block = format_clusters_for_prompt(ideas, clusters)
    prompt = (
        _SYNTHESIS_PROMPT
        .replace("{topic}", topic)
        .replace("{personas_block}", personas_block)
        .replace("{total_ideas}", str(len(ideas)))
        .replace("{clusters_block}", clusters_block)
        .replace("{top_n}", str(top_n))
    )
    result = provider.run(prompt, cwd=cwd, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        return "", result
    return (result.output or "").strip(), result


def format_clusters_for_prompt(
    ideas: list[BrainstormIdea],
    clusters: list[list[int]],
) -> str:
    """Render clusters as a Markdown-ish block to inject into the synthesis prompt."""
    parts: list[str] = []
    for cid, idx_list in enumerate(clusters):
        if not idx_list:
            continue
        cluster_personas = sorted({ideas[i].persona_key for i in idx_list})
        parts.append(
            f"### Cluster #{cid} (size={len(idx_list)}, personas={', '.join(cluster_personas)})"
        )
        for i in idx_list:
            idea = ideas[i]
            parts.append(f"  - [{idea.persona_key}, iter {idea.iteration}] {idea.text}")
    return "\n".join(parts)


# ── Report builder ───────────────────────────────────────────────────


def build_report(
    *,
    topic: str,
    personas: list[BrainstormPersona],
    allocations: list[BrainstormAllocation],
    ideas: list[BrainstormIdea],
    clusters: list[list[int]],
    iteration_history: list[ConvergenceResult],
    synthesis_md: str,
    converged: bool,
    iterations_used: int,
) -> str:
    """Assemble the final brainstorm Markdown report body (without header)."""
    provider_by_key = {alloc.persona.key: alloc.provider_name for alloc in allocations}
    persona_lines: list[str] = []
    for p in personas:
        provider = provider_by_key.get(p.key, "?")
        persona_lines.append(
            f"- **{p.name}** (`{p.key}`, provider: `{provider}`)\n"
            f"  - Rolle: {p.role_description}\n"
            f"  - Fokus: {p.perspective_focus}"
        )
    persona_block = "\n".join(persona_lines) or "_keine Personas_"

    iter_lines = []
    for i, conv in enumerate(iteration_history, start=1):
        iter_lines.append(
            f"- Runde {i}: {conv.cluster_count_after} Cluster "
            f"(+{conv.new_clusters} neu, {conv.new_cluster_ratio:.0%})"
        )
    iter_block = "\n".join(iter_lines) if iter_lines else "_keine Runden gelaufen_"

    cluster_block = format_clusters_for_prompt(ideas, clusters) or "_keine Cluster_"

    status = (
        "konvergiert" if converged
        else f"hard-cap erreicht ({iterations_used} Iterationen)"
    )

    return (
        f"## Thema\n\n{topic}\n\n"
        f"## Personas\n\n{persona_block}\n\n"
        f"{synthesis_md.strip() or '_keine Synthese_'}\n\n"
        f"---\n\n"
        f"## Cluster-Map (alle Ideen, gruppiert)\n\n"
        f"{cluster_block}\n\n"
        f"## Iterations-Statistik\n\n"
        f"- Status: **{status}**\n"
        f"- Iterationen genutzt: {iterations_used}\n"
        f"- Gesamt-Ideen: {len(ideas)}\n"
        f"- Gesamt-Cluster: {len(clusters)}\n\n"
        f"{iter_block}\n"
    )
