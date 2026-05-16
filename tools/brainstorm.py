"""Brainstorm Tool — multi-persona round-table with domain-aware personas.

Workflow
--------
Phase 0:   LLM analyses the topic, picks 4-6 domain-aware personas.
Phase 0.5: Allocate personas to providers (round-robin with #cross-provider tag,
           else primary-only).
Phase 1:   Each persona generates initial ideas independently.
Phase 2:   Cross-pollination — each persona sees peers' ideas and contributes new
           ones. Repeat until convergence (TF-IDF cluster-growth ratio below
           threshold) or hard-cap (default 5) iterations.
Phase 3:   Synthesizer (primary provider) picks Top-N from clusters with
           Pro/Contra.

Output
------
* Final report: ``{cwd}/docs/brainstorm-YYYYMMDD-HHMMSS.md``
* Per-iteration files + state.json: ``{cwd}/.brainstorm/{run_id}/``
* JSONL action trace: ``{cwd}/.brainstorm/traces/{trace_uuid}.jsonl``

Tags
----
* ``#tool:brainstorm`` (required, stripped by queue_manager)
* ``#cross-provider``  — opt-in: round-robin personas across all providers
* ``#max_iterations:N`` — override hard-cap (default 5)
* ``#top_n:N`` — override synthesis output size (default 5)
* ``#min_personas:N`` / ``#max_personas:N`` — constrain persona count (default 4-6)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config import (
    TOOL_BS_CLUSTER_SIMILARITY_THRESHOLD,
    TOOL_BS_CONVERGENCE_THRESHOLD,
    TOOL_BS_DEFAULT_TOP_N,
    TOOL_BS_MAX_IDEAS_PER_PERSONA_PER_ROUND,
    TOOL_BS_MAX_ITERATIONS,
    TOOL_BS_MAX_PEER_CHARS_PER_PERSONA,
    TOOL_BS_MAX_PERSONAS,
    TOOL_BS_MAX_TOTAL_INJECT_CHARS,
    TOOL_BS_MIN_PERSONAS,
    TOOL_BS_PERSONA_TIMEOUT_SEC,
    TOOL_BS_PHASE0_TIMEOUT_SEC,
    TOOL_BS_SYNTHESIS_TIMEOUT_SEC,
)
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from tools.base_tool import (
    BaseTool,
    TokenCounter,
    ToolResult,
    ToolTracer,
    _make_capacity_exhausted_result,
    _make_report_header,
    _write_tool_file,
)
from tools.brainstorm_phases import (
    BrainstormAllocation,
    BrainstormIdea,
    BrainstormPersona,
    ConvergenceResult,
    build_report,
    check_convergence,
    cluster_ideas,
    phase_cross_pollination,
    phase_idea_generation,
    phase_provider_allocation,
    phase_synthesis,
    phase_topic_analysis,
)

logger = logging.getLogger(__name__)


# ── Tag detection / stripping ─────────────────────────────────────────


_CROSS_PROVIDER_RE = re.compile(r"(?i)(?<!\S)#cross-provider(?=\s|$)")
_MAX_ITER_RE = re.compile(r"(?i)(?<!\S)#max_iterations:(\d+)(?=\s|$)")
_TOP_N_RE = re.compile(r"(?i)(?<!\S)#top_n:(\d+)(?=\s|$)")
_MIN_PERSONAS_RE = re.compile(r"(?i)(?<!\S)#min_personas:(\d+)(?=\s|$)")
_MAX_PERSONAS_RE = re.compile(r"(?i)(?<!\S)#max_personas:(\d+)(?=\s|$)")


def _wants_cross_provider(task: str) -> bool:
    return bool(_CROSS_PROVIDER_RE.search(task))


def _parse_int_tag(task: str, pattern: re.Pattern[str], default: int, *, lo: int, hi: int) -> int:
    m = pattern.search(task)
    if not m:
        return default
    try:
        val = int(m.group(1))
    except ValueError:
        return default
    return max(lo, min(hi, val))


def _clean_tags(task: str) -> str:
    """Strip brainstorm-specific tags from task text."""
    for pat in (
        _CROSS_PROVIDER_RE, _MAX_ITER_RE, _TOP_N_RE,
        _MIN_PERSONAS_RE, _MAX_PERSONAS_RE,
    ):
        task = pat.sub("", task)
    return " ".join(task.split())


# ── State persistence ────────────────────────────────────────────────


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write (.tmp → rename). Mirrors scientific_investigation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass
class _RunConfig:
    cross_provider: bool
    max_iterations: int
    top_n: int
    min_personas: int
    max_personas: int


# ── Tool ──────────────────────────────────────────────────────────────


class BrainstormTool(BaseTool):
    name = "brainstorm"
    description = (
        "Brainstorming Round-Table mit 4-6 domain-aware Personas; iterative "
        "Cross-Pollination bis Konvergenz, Synthesizer wählt Top-N mit Pro/Contra. "
        "Output → docs/brainstorm-*.md"
    )
    read_only = True  # All persona/synthesis calls run with read_only=True;
                      # the report file is written via Path.write_text (tool I/O).

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        *,
        provider_lookup: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> ToolResult:
        cfg = _RunConfig(
            cross_provider=_wants_cross_provider(task),
            max_iterations=_parse_int_tag(
                task, _MAX_ITER_RE, TOOL_BS_MAX_ITERATIONS, lo=1, hi=10,
            ),
            top_n=_parse_int_tag(
                task, _TOP_N_RE, TOOL_BS_DEFAULT_TOP_N, lo=1, hi=20,
            ),
            min_personas=_parse_int_tag(
                task, _MIN_PERSONAS_RE, TOOL_BS_MIN_PERSONAS, lo=2, hi=10,
            ),
            max_personas=_parse_int_tag(
                task, _MAX_PERSONAS_RE, TOOL_BS_MAX_PERSONAS, lo=2, hi=10,
            ),
        )
        if cfg.min_personas > cfg.max_personas:
            cfg.max_personas = cfg.min_personas

        topic = _clean_tags(task)
        if not topic:
            return ToolResult(
                success=False,
                error="Brainstorming-Topic ist leer (alle Tags wurden entfernt)",
                error_code="empty_topic",
            )

        cwd_path = Path(cwd) if cwd else Path.cwd()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = cwd_path / f".{self.name}" / timestamp
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        docs_dir = cwd_path / "docs"

        tracer = ToolTracer.create(self.name, cwd)
        counter = TokenCounter()

        tracer.emit(
            "run_start",
            topic=topic[:200],
            provider=provider.name,
            cross_provider=cfg.cross_provider,
            max_iterations=cfg.max_iterations,
            top_n=cfg.top_n,
            min_personas=cfg.min_personas,
            max_personas=cfg.max_personas,
        )

        state: dict[str, Any] = {
            "version": 1,
            "run_id": timestamp,
            "topic": topic,
            "primary_provider": provider.name,
            "cross_provider": cfg.cross_provider,
            "max_iterations": cfg.max_iterations,
            "top_n": cfg.top_n,
            "min_personas": cfg.min_personas,
            "max_personas": cfg.max_personas,
            "phase": "started",
            "personas": [],
            "allocations": [],
            "ideas": [],
            "iteration_history": [],
            "converged": False,
            "synthesis_md": "",
            "report_path": "",
        }
        _atomic_write_json(state_path, state)

        # ── Phase 0: persona generation ──────────────────────────
        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfuegbar bei Phase 0 (Persona-Generierung)"
            tracer.emit("capacity_exhausted", phase="phase0")
            tracer.emit("run_end", success=False, reason="capacity_exhausted_phase0")
            return _make_capacity_exhausted_result(
                msg, "", 0, **counter.as_kwargs(),
            )

        notify_tool_progress(self.name, 1, 4, "Phase 0/3: Personas generieren...")
        tracer.emit("phase_start", phase="phase0_personas")
        try:
            personas, p0_result = phase_topic_analysis(
                topic, provider,
                min_personas=cfg.min_personas,
                max_personas=cfg.max_personas,
                timeout_sec=TOOL_BS_PHASE0_TIMEOUT_SEC,
                cwd=str(cwd_path),
            )
        except (RuntimeError, ValueError) as exc:
            err = str(exc)
            logger.warning("brainstorm phase0 failed: %s", err)
            tracer.emit("run_end", success=False, reason="phase0_failed", error=err[:200])
            notify_tool_done(self.name, 0, False, f"Phase 0 fehlgeschlagen: {err[:120]}")
            return ToolResult(
                success=False,
                output="",
                iterations=0,
                error=err,
                error_code="phase0_failed",
                **counter.as_kwargs(),
            )
        counter.add(p0_result)
        tracer.emit(
            "phase_end",
            phase="phase0_personas",
            success=True,
            personas=[p.key for p in personas],
        )
        state["personas"] = [p.as_dict() for p in personas]
        state["phase"] = "phase0_done"
        _atomic_write_json(state_path, state)
        print(f"  [{self.name}] Phase 0: {len(personas)} Personas generiert "
              f"({', '.join(p.key for p in personas)})")

        # ── Phase 0.5: provider allocation ───────────────────────
        try:
            allocations = phase_provider_allocation(
                personas,
                primary_provider_name=provider.name,
                cross_provider=cfg.cross_provider,
                provider_lookup=provider_lookup,
            )
        except Exception as exc:  # provider_lookup may raise — degrade
            logger.warning("brainstorm allocation failed, falling back to primary-only: %s", exc)
            allocations = [
                BrainstormAllocation(persona=p, provider_name=provider.name)
                for p in personas
            ]
        state["allocations"] = [a.as_dict() for a in allocations]
        state["phase"] = "phase05_done"
        _atomic_write_json(state_path, state)

        # Resolve providers once. For primary-only mode, all entries point to
        # the same `provider` instance.
        if provider_lookup is None:
            from dispatcher import get_provider_by_name as _lookup
            provider_lookup = _lookup

        provider_by_alloc: list[BaseProvider] = []
        for alloc in allocations:
            if alloc.provider_name == provider.name:
                provider_by_alloc.append(provider)
                continue
            resolved = provider_lookup(alloc.provider_name)
            if resolved is None:
                logger.warning(
                    "brainstorm: provider %s for persona %s unavailable, falling back to %s",
                    alloc.provider_name, alloc.persona.key, provider.name,
                )
                provider_by_alloc.append(provider)
            else:
                provider_by_alloc.append(resolved)

        # ── Phase 1 + 2 loop: iterative idea generation ──────────
        ideas: list[BrainstormIdea] = []
        iteration_history: list[ConvergenceResult] = []
        clusters: list[list[int]] = []
        converged = False
        prev_cluster_count = 0

        for iteration in range(1, cfg.max_iterations + 1):
            is_first = iteration == 1
            phase_label = "phase1_initial" if is_first else f"phase2_iter{iteration}"
            notify_tool_progress(
                self.name,
                iteration + 1, cfg.max_iterations + 2,
                f"Iteration {iteration}: {len(personas)} Personas, "
                + ("Initial" if is_first else "Cross-Pollination") + "...",
            )
            tracer.emit("iteration_start", iteration=iteration, mode=phase_label)

            iteration_ideas: list[BrainstormIdea] = []
            for alloc, sub_provider in zip(allocations, provider_by_alloc):
                if not is_cached_provider_available(sub_provider.name):
                    msg = (
                        f"Provider {sub_provider.name} erschoepft in Iteration "
                        f"{iteration} bei Persona {alloc.persona.key}"
                    )
                    tracer.emit(
                        "capacity_exhausted",
                        phase=phase_label,
                        iteration=iteration,
                        persona=alloc.persona.key,
                        provider=sub_provider.name,
                    )
                    ideas.extend(iteration_ideas)
                    state["ideas"] = [i.as_dict() for i in ideas]
                    state["phase"] = f"iter{iteration}_partial"
                    _atomic_write_json(state_path, state)
                    tracer.emit("run_end", success=False, reason="capacity_exhausted")
                    return _make_capacity_exhausted_result(
                        msg, self._format_partial(ideas), iteration,
                        **counter.as_kwargs(),
                    )

                tracer.emit(
                    "subprocess_call",
                    phase=phase_label,
                    persona=alloc.persona.key,
                    provider=sub_provider.name,
                    iteration=iteration,
                )

                if is_first:
                    raw_ideas, sub_result = phase_idea_generation(
                        alloc.persona, sub_provider,
                        topic=topic,
                        max_ideas=TOOL_BS_MAX_IDEAS_PER_PERSONA_PER_ROUND,
                        timeout_sec=TOOL_BS_PERSONA_TIMEOUT_SEC,
                        cwd=str(cwd_path),
                    )
                else:
                    own_ideas = [i for i in ideas if i.persona_key == alloc.persona.key]
                    peer_map: dict[str, list[BrainstormIdea]] = {}
                    for peer in allocations:
                        if peer.persona.key == alloc.persona.key:
                            continue
                        peer_map[peer.persona.key] = [
                            i for i in ideas if i.persona_key == peer.persona.key
                        ]
                    raw_ideas, sub_result = phase_cross_pollination(
                        alloc.persona, sub_provider,
                        topic=topic,
                        iteration=iteration,
                        own_ideas=own_ideas,
                        peer_ideas_by_persona=peer_map,
                        max_ideas=TOOL_BS_MAX_IDEAS_PER_PERSONA_PER_ROUND,
                        timeout_sec=TOOL_BS_PERSONA_TIMEOUT_SEC,
                        max_peer_chars_per_persona=TOOL_BS_MAX_PEER_CHARS_PER_PERSONA,
                        max_total_inject_chars=TOOL_BS_MAX_TOTAL_INJECT_CHARS,
                        cwd=str(cwd_path),
                    )

                counter.add(sub_result)

                tracer.emit(
                    "subprocess_result",
                    phase=phase_label,
                    persona=alloc.persona.key,
                    success=bool(getattr(sub_result, "success", False)),
                    ideas_count=len(raw_ideas),
                    input_tokens=getattr(sub_result, "input_tokens", 0),
                    output_tokens=getattr(sub_result, "output_tokens", 0),
                )

                if not getattr(sub_result, "success", False):
                    err = getattr(sub_result, "error", "unknown")
                    print(f"  [{self.name}] WARN Persona {alloc.persona.key} "
                          f"(iter {iteration}) failed: {err}")
                    continue

                for text in raw_ideas:
                    iteration_ideas.append(BrainstormIdea(
                        text=text,
                        persona_key=alloc.persona.key,
                        iteration=iteration,
                    ))

                # Save per-persona iteration file
                self._write_iteration_file(
                    run_dir, iteration, alloc.persona, raw_ideas,
                )

            ideas.extend(iteration_ideas)

            # Re-cluster all ideas accumulated so far
            clusters = cluster_ideas(
                ideas,
                similarity_threshold=TOOL_BS_CLUSTER_SIMILARITY_THRESHOLD,
            )
            conv = check_convergence(
                prev_cluster_count,
                clusters,
                threshold=TOOL_BS_CONVERGENCE_THRESHOLD,
            )
            iteration_history.append(conv)
            prev_cluster_count = len(clusters)

            tracer.emit(
                "iteration_end",
                iteration=iteration,
                ideas_total=len(ideas),
                clusters=conv.cluster_count_after,
                new_clusters=conv.new_clusters,
                new_cluster_ratio=round(conv.new_cluster_ratio, 4),
                converged=conv.converged,
            )

            state["ideas"] = [i.as_dict() for i in ideas]
            state["iteration_history"] = [c.as_dict() for c in iteration_history]
            state["phase"] = f"iter{iteration}_done"
            _atomic_write_json(state_path, state)

            print(
                f"  [{self.name}] Iter {iteration} done: {len(iteration_ideas)} neue Ideen, "
                f"{conv.cluster_count_after} Cluster (+{conv.new_clusters}), "
                f"new-ratio={conv.new_cluster_ratio:.0%}"
            )

            if conv.converged:
                converged = True
                print(f"  [{self.name}] Konvergiert nach Iteration {iteration}")
                break

        iterations_used = len(iteration_history)
        state["converged"] = converged
        state["phase"] = "iterations_done"
        _atomic_write_json(state_path, state)

        if not ideas:
            msg = "Keine Ideen generiert — alle Personas fehlgeschlagen"
            tracer.emit("run_end", success=False, reason="no_ideas")
            notify_tool_done(self.name, iterations_used, False, msg)
            return ToolResult(
                success=False,
                output="",
                iterations=iterations_used,
                error=msg,
                error_code="no_ideas",
                **counter.as_kwargs(),
            )

        # ── Phase 3: synthesis ───────────────────────────────────
        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfuegbar bei Phase 3 (Synthesis) — Ideen gespeichert"
            tracer.emit("capacity_exhausted", phase="phase3_synthesis")
            tracer.emit("run_end", success=False, reason="capacity_exhausted_synthesis")
            return _make_capacity_exhausted_result(
                msg, self._format_partial(ideas), iterations_used,
                **counter.as_kwargs(),
            )

        notify_tool_progress(
            self.name, iterations_used + 2, cfg.max_iterations + 2,
            "Phase 3/3: Synthese + Ranking...",
        )
        tracer.emit("phase_start", phase="phase3_synthesis")
        synthesis_md, synth_result = phase_synthesis(
            provider,
            topic=topic,
            personas=personas,
            ideas=ideas,
            clusters=clusters,
            top_n=cfg.top_n,
            timeout_sec=TOOL_BS_SYNTHESIS_TIMEOUT_SEC,
            cwd=str(cwd_path),
        )
        counter.add(synth_result)

        if not getattr(synth_result, "success", False):
            err = getattr(synth_result, "error", "unknown")
            tracer.emit("phase_end", phase="phase3_synthesis", success=False, error=err[:200])
            tracer.emit("run_end", success=False, reason="synthesis_failed")
            # Still save a report with raw clusters so the user has something
            report_body = build_report(
                topic=topic, personas=personas, allocations=allocations,
                ideas=ideas, clusters=clusters,
                iteration_history=iteration_history,
                synthesis_md=f"_Synthese fehlgeschlagen: {err[:200]}_",
                converged=converged, iterations_used=iterations_used,
            )
            report_path = self._write_report(
                docs_dir, timestamp, topic, provider, cwd_path, report_body,
            )
            state["report_path"] = str(report_path)
            state["phase"] = "synthesis_failed"
            _atomic_write_json(state_path, state)
            notify_tool_done(self.name, iterations_used, False,
                             f"Synthese fehlgeschlagen, Raw-Report: {report_path.name}")
            return ToolResult(
                success=False,
                output=f"Synthese fehlgeschlagen — Raw-Report: {report_path}",
                iterations=iterations_used,
                error=err,
                error_code=getattr(synth_result, "error", "synthesis_failed") or "synthesis_failed",
                retryable=False,
                **counter.as_kwargs(),
            )

        tracer.emit("phase_end", phase="phase3_synthesis", success=True,
                    output_chars=len(synthesis_md))

        # ── Build final report ──────────────────────────────────
        report_body = build_report(
            topic=topic, personas=personas, allocations=allocations,
            ideas=ideas, clusters=clusters,
            iteration_history=iteration_history,
            synthesis_md=synthesis_md,
            converged=converged, iterations_used=iterations_used,
        )
        report_path = self._write_report(
            docs_dir, timestamp, topic, provider, cwd_path, report_body,
        )

        state["synthesis_md"] = synthesis_md
        state["report_path"] = str(report_path)
        state["phase"] = "complete"
        _atomic_write_json(state_path, state)

        tracer.emit("run_end", success=True,
                    ideas_total=len(ideas),
                    clusters=len(clusters),
                    iterations=iterations_used,
                    converged=converged,
                    report=str(report_path))
        notify_tool_done(
            self.name, iterations_used, True,
            f"{len(ideas)} Ideen, {len(clusters)} Cluster -> {report_path.name}",
        )
        return ToolResult(
            success=True,
            output=f"Brainstorm fertig: {report_path}",
            iterations=iterations_used,
            **counter.as_kwargs(),
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _write_iteration_file(
        self,
        run_dir: Path,
        iteration: int,
        persona: BrainstormPersona,
        ideas: list[str],
    ) -> None:
        """Save raw per-persona ideas for one iteration (safety net)."""
        fname = f"iteration-{iteration}-{persona.key}.md"
        body = (
            f"# Iteration {iteration} — {persona.name} (`{persona.key}`)\n\n"
            + "\n".join(f"{i + 1}. {text}" for i, text in enumerate(ideas))
            + ("\n" if ideas else "_keine Ideen_\n")
        )
        try:
            (run_dir / fname).write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning("brainstorm: iteration file write failed (%s): %s", fname, exc)

    def _write_report(
        self,
        docs_dir: Path,
        timestamp: str,
        topic: str,
        provider: BaseProvider,
        cwd_path: Path,
        body: str,
    ) -> Path:
        filename = f"brainstorm-{timestamp}.md"
        header = _make_report_header(
            "Brainstorm — Round-Table",
            timestamp, topic, provider.name, cwd_path,
        )
        _write_tool_file(docs_dir, filename, header + body)
        return docs_dir / filename

    @staticmethod
    def _format_partial(ideas: list[BrainstormIdea]) -> str:
        """Format ideas as text for partial-result output (capacity exhaustion)."""
        if not ideas:
            return "[keine Ideen]"
        by_persona: dict[str, list[BrainstormIdea]] = {}
        for idea in ideas:
            by_persona.setdefault(idea.persona_key, []).append(idea)
        parts: list[str] = []
        for key, persona_ideas in by_persona.items():
            parts.append(f"## {key}")
            for idea in persona_ideas:
                parts.append(f"  - [iter {idea.iteration}] {idea.text}")
        return "\n".join(parts)
