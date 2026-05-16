"""Unit tests for the Brainstorm tool.

Coverage:
  * Tag parsing (#cross-provider, #max_iterations:N, #top_n:N, #min/max_personas:N)
  * Tag stripping (_clean_tags)
  * parse_personas validation (count range, unique keys, system_prompt length)
  * parse_ideas (```ideas block + fallback)
  * phase_provider_allocation (primary-only + round-robin + degraded)
  * cluster_ideas (Jaccard-cosine threshold)
  * check_convergence (first round + below threshold)
  * build_report (sections present)
  * Full run via scripted provider (happy path)
  * Failed-persona continues with rest
  * Empty topic guard
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    from providers.base import RunResult
    from tools.brainstorm import (
        BrainstormTool,
        _clean_tags,
        _parse_int_tag,
        _wants_cross_provider,
        _MAX_ITER_RE,
        _MAX_PERSONAS_RE,
        _MIN_PERSONAS_RE,
        _TOP_N_RE,
    )
    from tools.brainstorm_phases import (
        BrainstormAllocation,
        BrainstormIdea,
        BrainstormPersona,
        build_report,
        check_convergence,
        cluster_ideas,
        parse_ideas,
        parse_personas,
        phase_provider_allocation,
    )


# ── Helpers ──────────────────────────────────────────────────────────


class _ScriptedProvider:
    """Returns scripted outputs in sequence; records each .run() call."""

    def __init__(self, name: str, outputs: list[str], supports_sessions: bool = False):
        self.name = name
        self._outputs = list(outputs)
        self.calls: list[dict] = []
        self.supports_sessions = supports_sessions

    def run(self, task, cwd=None, timeout=0, read_only=False, **kwargs):
        self.calls.append({
            "task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only,
        })
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _noop(*_args, **_kwargs):
    pass


@pytest.fixture
def _patch(monkeypatch):
    monkeypatch.setattr("tools.brainstorm.notify_tool_done", _noop)
    monkeypatch.setattr("tools.brainstorm.notify_tool_progress", _noop)
    monkeypatch.setattr("tools.brainstorm.is_cached_provider_available", lambda _name: True)


# YAML output the LLM is supposed to produce in Phase 0 — single-line system_prompt
# so the minimal-YAML fallback parser handles it (no block scalars / multiline).
_PERSONAS_YAML = """\
```yaml
personas:
  - key: data-analyst
    name: Daten-Analyst
    role_description: Schaut auf Conversion-Rates und Margen
    perspective_focus: Datenbasiert messbar
    system_prompt: "Du bist Daten-Analyst mit Fokus auf quantitative Pricing-Daten. Du argumentierst nie emotional. Du fragst immer wie etwas messbar ist und wo der Datenpunkt herkommt. Du fokussierst auf konkrete KPIs."
  - key: storefront
    name: Boutique-Verkaeuferin
    role_description: Beratet Kunden im Laden
    perspective_focus: Bauchgefuehl im direkten Verkauf
    system_prompt: "Du bist Boutique-Verkaeuferin mit zehn Jahren Erfahrung am Tresen. Du kennst Kundinnen-Reaktionen aus dem Stand. Du argumentierst aus konkreten Verkaufsgespraechen heraus. Du misstraust reinen Datenpunkten."
  - key: competitor
    name: Mitbewerber
    role_description: Schaut von aussen auf die Konkurrenz
    perspective_focus: Markt-Vergleich
    system_prompt: "Du bist anonymer Mitbewerber aus der gleichen Region. Du beobachtest scharf was der andere Laden anders macht. Du argumentierst aus Preis-Vergleichen und Sortiments-Differenzen. Du bist pragmatisch und ergebnisorientiert."
  - key: bride
    name: Braut-Kundin
    role_description: Vertritt die Zielgruppe
    perspective_focus: Kundinnen-Empfinden im Kaufprozess
    system_prompt: "Du bist Braut Mitte 20 die das erste mal ein teures Kleid kauft. Du achtest auf Atmosphaere Service Beratung und Preis-Leistung. Du argumentierst emotional aber auch praktisch zum Budget. Du erzaehlst gern aus deiner Sicht."
```
"""


def _ideas_block(*ideas: str) -> str:
    body = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
    return f"```ideas\n{body}\n```"


# ── Tag parsing ──────────────────────────────────────────────────────


class TestTagParsing:

    def test_wants_cross_provider_default_off(self):
        assert _wants_cross_provider("Brainstorm pricing strategie") is False

    def test_wants_cross_provider_with_tag(self):
        assert _wants_cross_provider("Brainstorm #cross-provider") is True

    def test_wants_cross_provider_word_boundary(self):
        # tag must not match in random substrings
        assert _wants_cross_provider("see foo#cross-provider") is False
        assert _wants_cross_provider("#cross-provider-foo") is False

    def test_parse_int_tag_max_iterations(self):
        assert _parse_int_tag("foo #max_iterations:3 bar", _MAX_ITER_RE, 5, lo=1, hi=10) == 3

    def test_parse_int_tag_top_n_default_when_missing(self):
        assert _parse_int_tag("no tag here", _TOP_N_RE, 5, lo=1, hi=20) == 5

    def test_parse_int_tag_clamps_to_range(self):
        # value 99 must clamp to hi=10
        assert _parse_int_tag("x #max_iterations:99", _MAX_ITER_RE, 5, lo=1, hi=10) == 10
        # value 0 must clamp to lo=1
        assert _parse_int_tag("x #max_iterations:0", _MAX_ITER_RE, 5, lo=1, hi=10) == 1

    def test_parse_int_tag_min_personas(self):
        assert _parse_int_tag("x #min_personas:3", _MIN_PERSONAS_RE, 4, lo=2, hi=10) == 3

    def test_parse_int_tag_max_personas(self):
        assert _parse_int_tag("x #max_personas:7", _MAX_PERSONAS_RE, 6, lo=2, hi=10) == 7

    def test_clean_tags_removes_all_brainstorm_tags(self):
        out = _clean_tags(
            "Brainstorm pricing #cross-provider #max_iterations:3 #top_n:7 "
            "#min_personas:3 #max_personas:5"
        )
        for tag in ("#cross-provider", "#max_iterations", "#top_n", "#min_personas", "#max_personas"):
            assert tag not in out
        assert "Brainstorm pricing" in out

    def test_clean_tags_no_tags_unchanged(self):
        assert _clean_tags("Brainstorm pricing strategy") == "Brainstorm pricing strategy"


# ── parse_personas ───────────────────────────────────────────────────


class TestParsePersonas:

    def _good_persona(self, key: str = "demo") -> dict:
        return {
            "key": key,
            "name": "Demo",
            "role_description": "x",
            "perspective_focus": "y",
            "system_prompt": "Du bist Demo. " + "A" * 100,
        }

    def test_too_few_personas_rejected(self):
        parsed = {"personas": [self._good_persona("a"), self._good_persona("b")]}
        with pytest.raises(ValueError, match="outside allowed range"):
            parse_personas(parsed, min_personas=4, max_personas=6)

    def test_too_many_personas_rejected(self):
        personas = [self._good_persona(f"p{i}") for i in range(7)]
        with pytest.raises(ValueError, match="outside allowed range"):
            parse_personas({"personas": personas}, min_personas=4, max_personas=6)

    def test_duplicate_key_rejected(self):
        personas = [
            self._good_persona("a"), self._good_persona("a"),
            self._good_persona("b"), self._good_persona("c"),
        ]
        with pytest.raises(ValueError, match="duplicate persona key"):
            parse_personas({"personas": personas}, min_personas=4, max_personas=6)

    def test_invalid_key_rejected(self):
        bad = self._good_persona("Bad_Key!")  # uppercase + underscore + bang
        personas = [bad] + [self._good_persona(f"p{i}") for i in range(3)]
        with pytest.raises(ValueError, match="must be kebab-case"):
            parse_personas({"personas": personas}, min_personas=4, max_personas=6)

    def test_short_system_prompt_rejected(self):
        bad = self._good_persona("a")
        bad["system_prompt"] = "Zu kurz."  # < 100 chars
        personas = [bad] + [self._good_persona(f"p{i}") for i in range(3)]
        with pytest.raises(ValueError, match="system_prompt too short"):
            parse_personas({"personas": personas}, min_personas=4, max_personas=6)

    def test_happy_path_returns_persona_objects(self):
        personas = [self._good_persona(f"p{i}") for i in range(4)]
        out = parse_personas({"personas": personas}, min_personas=4, max_personas=6)
        assert len(out) == 4
        assert all(isinstance(p, BrainstormPersona) for p in out)
        assert [p.key for p in out] == ["p0", "p1", "p2", "p3"]

    def test_missing_personas_key_rejected(self):
        with pytest.raises(ValueError, match="missing 'personas'"):
            parse_personas({"foo": "bar"}, min_personas=4, max_personas=6)


# ── parse_ideas ──────────────────────────────────────────────────────


class TestParseIdeas:

    def test_extracts_numbered_ideas_in_fence(self):
        text = _ideas_block("idea one", "idea two", "idea three")
        assert parse_ideas(text, max_ideas=10) == ["idea one", "idea two", "idea three"]

    def test_respects_max_ideas_cap(self):
        text = _ideas_block(*[f"idea {i}" for i in range(20)])
        assert len(parse_ideas(text, max_ideas=5)) == 5

    def test_fallback_to_freeform_numbered_list(self):
        text = "Here are my ideas:\n1. one\n2. two\n3. three\n"
        assert parse_ideas(text, max_ideas=10) == ["one", "two", "three"]

    def test_empty_text_returns_empty(self):
        assert parse_ideas("", max_ideas=5) == []

    def test_ignores_non_numbered_lines(self):
        text = "```ideas\nFoo bar baz\n1. first\nNot numbered\n2. second\n```"
        assert parse_ideas(text, max_ideas=5) == ["first", "second"]


# ── phase_provider_allocation ────────────────────────────────────────


def _personas(n: int) -> list[BrainstormPersona]:
    return [
        BrainstormPersona(
            key=f"p{i}", name=f"P{i}",
            role_description="r", perspective_focus="f",
            system_prompt="x" * 120,
        )
        for i in range(n)
    ]


class TestProviderAllocation:

    def test_primary_only_when_cross_provider_false(self):
        allocs = phase_provider_allocation(
            _personas(4),
            primary_provider_name="claude",
            cross_provider=False,
            provider_lookup=lambda _: object(),  # all available, but irrelevant
        )
        assert {a.provider_name for a in allocs} == {"claude"}
        assert len(allocs) == 4

    def test_round_robin_across_available_providers(self):
        # Lookup returns object for gemini + codex, None for openrouter
        def lookup(name):
            return object() if name in ("gemini", "codex") else None
        allocs = phase_provider_allocation(
            _personas(6),
            primary_provider_name="claude",
            cross_provider=True,
            provider_lookup=lookup,
        )
        names = [a.provider_name for a in allocs]
        # Available = [claude, gemini, codex] — round-robin
        assert names == ["claude", "gemini", "codex", "claude", "gemini", "codex"]

    def test_degrades_to_primary_when_no_cross_available(self):
        allocs = phase_provider_allocation(
            _personas(3),
            primary_provider_name="claude",
            cross_provider=True,
            provider_lookup=lambda _: None,  # nothing else available
        )
        assert all(a.provider_name == "claude" for a in allocs)


# ── cluster_ideas ────────────────────────────────────────────────────


class TestClusterIdeas:

    def test_similar_short_texts_cluster_together(self):
        ideas = [
            BrainstormIdea(text="Senke den Preis um zehn Prozent fuer Stammkunden", persona_key="a", iteration=1),
            BrainstormIdea(text="Reduziere den Preis um zehn Prozent fuer Stammkunden", persona_key="b", iteration=1),
            BrainstormIdea(text="Erweitere das Sortiment um Brautjungfern-Kleider", persona_key="c", iteration=1),
        ]
        clusters = cluster_ideas(ideas, similarity_threshold=0.4)
        # First two share most tokens — they should be in same cluster
        assert ideas[0].cluster_id == ideas[1].cluster_id
        # The sortiment idea should be in a different cluster
        assert ideas[2].cluster_id != ideas[0].cluster_id
        assert len(clusters) == 2

    def test_each_idea_gets_own_cluster_when_distinct(self):
        ideas = [
            BrainstormIdea(text="Brautmode Verkostung Boutique Erlebnis", persona_key="a", iteration=1),
            BrainstormIdea(text="Online Shop Versand Konfigurator Auswahl", persona_key="b", iteration=1),
            BrainstormIdea(text="Social Media Instagram Pinterest Werbung Reichweite", persona_key="c", iteration=1),
        ]
        clusters = cluster_ideas(ideas, similarity_threshold=0.4)
        assert len(clusters) == 3
        assert {i.cluster_id for i in ideas} == {0, 1, 2}

    def test_assigns_cluster_id_on_each_idea(self):
        ideas = [
            BrainstormIdea(text="alpha beta gamma delta", persona_key="a", iteration=1),
            BrainstormIdea(text="epsilon zeta eta theta", persona_key="b", iteration=1),
        ]
        cluster_ideas(ideas, similarity_threshold=0.4)
        assert all(i.cluster_id >= 0 for i in ideas)


# ── check_convergence ────────────────────────────────────────────────


class TestCheckConvergence:

    def test_first_round_never_converged(self):
        # previous_count=0 ⇒ entire cluster set is "new" ⇒ ratio=1.0 ⇒ not converged
        clusters = [[0, 1], [2], [3]]
        conv = check_convergence(0, clusters, threshold=0.2)
        assert conv.converged is False
        assert conv.new_clusters == 3
        assert conv.cluster_count_after == 3

    def test_converged_when_ratio_below_threshold(self):
        clusters = [[i] for i in range(11)]  # 11 clusters total
        # previous_count=10, current=11 ⇒ 1 new ⇒ ratio=1/11 ≈ 9% < 20%
        conv = check_convergence(10, clusters, threshold=0.2)
        assert conv.converged is True
        assert conv.new_clusters == 1

    def test_not_converged_when_ratio_above_threshold(self):
        clusters = [[i] for i in range(10)]
        # previous_count=5, current=10 ⇒ 5 new ⇒ ratio=50% > 20%
        conv = check_convergence(5, clusters, threshold=0.2)
        assert conv.converged is False
        assert conv.new_clusters == 5

    def test_empty_clusters_never_converged(self):
        conv = check_convergence(0, [], threshold=0.2)
        assert conv.converged is False
        assert conv.cluster_count_after == 0


# ── build_report ─────────────────────────────────────────────────────


class TestBuildReport:

    def test_report_contains_all_required_sections(self):
        personas = _personas(3)
        allocs = [BrainstormAllocation(persona=p, provider_name="claude") for p in personas]
        ideas = [
            BrainstormIdea(text="alpha beta gamma", persona_key="p0", iteration=1, cluster_id=0),
            BrainstormIdea(text="alpha beta gamma", persona_key="p1", iteration=1, cluster_id=0),
        ]
        report = build_report(
            topic="Mein Thema",
            personas=personas,
            allocations=allocs,
            ideas=ideas,
            clusters=[[0, 1]],
            iteration_history=[],
            synthesis_md="## Top-5 Ideen\n\n### 1. Foo",
            converged=False,
            iterations_used=2,
        )
        assert "## Thema" in report
        assert "## Personas" in report
        assert "## Cluster-Map" in report
        assert "## Iterations-Statistik" in report
        assert "Top-5 Ideen" in report
        assert "Mein Thema" in report
        assert "iteration" not in report.lower() or "Iterationen genutzt: 2" in report


# ── Full run ─────────────────────────────────────────────────────────


class TestBrainstormRun:

    def test_happy_path_with_convergence(self, tmp_path, _patch):
        """Phase 0 (1 call) + iter 1 (4 calls) + iter 2 (4 calls) + synthesis (1 call) = 10 calls."""
        # Iter 1: 4 distinct ideas → 4 clusters
        # Iter 2: same ideas repeated → 0 new clusters → converged
        same_idea_block_1 = _ideas_block(
            "brautmode boutique erlebnis service",
            "verkauf gespraech beratung persoenlich",
        )
        same_idea_block_2 = _ideas_block(
            "preis vergleich konkurrenz region passau",
            "online sortiment versand konfigurator",
        )
        same_idea_block_3 = _ideas_block(
            "kunde bewertung rezension google bewertung",
            "termin buchung kalender werkzeug verfuegbarkeit",
        )
        same_idea_block_4 = _ideas_block(
            "marketing instagram pinterest reichweite",
            "newsletter mailing kunden treue programm",
        )

        outputs = (
            [_PERSONAS_YAML]                       # phase 0
            + [same_idea_block_1, same_idea_block_2, same_idea_block_3, same_idea_block_4]  # iter 1
            + [same_idea_block_1, same_idea_block_2, same_idea_block_3, same_idea_block_4]  # iter 2 (same → converge)
            + ["## Top-3 Ideen\n\n### 1. Bessere Bewertungen\n- **Ursprung:** Cluster #2\n- **Kern-Idee:** ..."]  # synthesis
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = BrainstormTool()
        result = tool.run(
            "Pricing-Strategie WhiteLady #top_n:3",
            provider,
            cwd=str(tmp_path),
        )
        assert result.success is True, f"failed: {result.error}"
        # 1 (phase 0) + 4 (iter 1) + 4 (iter 2) + 1 (synthesis) = 10 calls
        assert len(provider.calls) == 10
        # Report exists
        reports = list((tmp_path / "docs").glob("brainstorm-*.md"))
        assert len(reports) == 1
        text = reports[0].read_text(encoding="utf-8")
        assert "Top-3 Ideen" in text
        assert "Pricing-Strategie WhiteLady" in text
        # State file with phase=complete
        state_files = list((tmp_path / ".brainstorm").rglob("state.json"))
        assert len(state_files) == 1
        import json
        state = json.loads(state_files[0].read_text(encoding="utf-8"))
        assert state["phase"] == "complete"
        assert state["converged"] is True
        # Per-iteration persona files exist
        iter_files = list((tmp_path / ".brainstorm").rglob("iteration-*.md"))
        assert len(iter_files) == 8  # 4 personas × 2 iterations

    def test_failed_persona_does_not_abort_run(self, tmp_path, _patch, monkeypatch):
        """If one persona's LLM call fails, the others continue."""

        # Mock provider that fails on the SECOND call (first persona in iter 1)
        class _PartiallyFailingProvider:
            def __init__(self):
                self.name = "claude"
                self.supports_sessions = False
                self._calls = 0
                self.calls = []

            def run(self, task, cwd=None, timeout=0, read_only=False, **kwargs):
                self._calls += 1
                self.calls.append({"task": task[:50]})
                # Call 1: persona YAML
                if self._calls == 1:
                    return RunResult(success=True, output=_PERSONAS_YAML)
                # Call 2: first persona iter 1 → fail
                if self._calls == 2:
                    return RunResult(success=False, error="rate_limit")
                # Calls 3-5: remaining personas iter 1 → ok
                if self._calls <= 5:
                    return RunResult(
                        success=True,
                        output=_ideas_block(f"idea-from-call-{self._calls}-alpha"),
                    )
                # Calls 6-9: iter 2 (same to converge)
                if self._calls <= 9:
                    return RunResult(
                        success=True,
                        output=_ideas_block(f"idea-from-call-{self._calls - 4}-alpha"),
                    )
                # Synthesis
                return RunResult(success=True, output="## Top-3 Ideen\n\n### 1. Foo")

        provider = _PartiallyFailingProvider()
        tool = BrainstormTool()
        result = tool.run("Pricing test", provider, cwd=str(tmp_path))
        # Tool must NOT fail just because one persona crashed
        assert result.success is True, f"unexpectedly failed: {result.error}"
        # 4 personas × 2 iter + 1 phase0 + 1 synth = 10 calls (one was a failure but still counted)
        assert provider._calls >= 8  # at least most calls happened

    def test_empty_topic_returns_error(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", [])
        tool = BrainstormTool()
        # Task containing ONLY tags → topic becomes empty after stripping
        result = tool.run("#cross-provider #top_n:5", provider, cwd=str(tmp_path))
        assert result.success is False
        assert result.error_code == "empty_topic"
        # No LLM call should have happened
        assert len(provider.calls) == 0

    def test_phase0_failure_returns_phase0_failed(self, tmp_path, _patch):
        """When the LLM returns invalid YAML, the tool fails cleanly."""
        outputs = ["This is not YAML at all."]
        provider = _ScriptedProvider("claude", outputs)
        tool = BrainstormTool()
        result = tool.run("Some topic", provider, cwd=str(tmp_path))
        assert result.success is False
        assert result.error_code == "phase0_failed"
        # Only the phase-0 call happened
        assert len(provider.calls) == 1

    def test_capacity_exhausted_before_phase0_is_retryable(self, tmp_path, monkeypatch):
        """If the provider is reported unavailable at startup, tool returns retryable."""
        monkeypatch.setattr("tools.brainstorm.notify_tool_done", _noop)
        monkeypatch.setattr("tools.brainstorm.notify_tool_progress", _noop)
        monkeypatch.setattr("tools.brainstorm.is_cached_provider_available", lambda _: False)
        provider = _ScriptedProvider("claude", [])
        tool = BrainstormTool()
        result = tool.run("Brainstorm topic", provider, cwd=str(tmp_path))
        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert result.retryable is True
        assert len(provider.calls) == 0
