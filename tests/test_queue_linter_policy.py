"""Tests for queue_linter's policy awareness (defect: --lint-queue was policy-blind).

Until 2026-09-09 the linter only ever asked whether a provider was REGISTERED
(binary on PATH, API key present). Nothing asked whether policy.yaml allowed it,
so a bare ``#opencode`` task under a ``default: [claude, codex]`` policy linted
clean and then ended terminal ❌ ``provider_not_allowed`` at 03:00 — a measured
total outage with no warning available beforehand.

The conftest fixture ``_isolate_policy_engine`` points the engine at an empty
``{}`` policy for every test, so each test here installs the policy it needs.

Every test whose queue line carries an ``#opencode`` tag also takes the shared
``with_opencode`` fixture. That is not decoration: ``resolve_forced_provider()``
returns None for an unregistered provider, so on a machine without the opencode
CLI ``forced_provider_policy_violation()`` finds nothing to report and the
assertions below would silently test nothing (measured — with
``dispatcher._providers.pop("opencode")`` they fail; the suite passes on this box
only because opencode is installed here).
"""

from pathlib import Path

import pytest

import policy as policy_module
import queue_linter
from queue_linter import LEVEL_ERROR, LEVEL_WARN, exit_code_for, lint_queue


def _codes(findings):
    return {f.code for f in findings}


def _install_policy(monkeypatch, tmp_path: Path, text: str | None, *, name="vault") -> Path:
    """Point the PolicyEngine singleton at a vault carrying *text* (None = no file)."""
    vault = Path(tmp_path) / name
    (vault / "99_System" / "AI").mkdir(parents=True, exist_ok=True)
    path: Path = vault / "99_System" / "AI" / "policy.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(policy_module, "_engine", policy_module.PolicyEngine(vault_path=vault))
    return path


_BARRING = """
tool_providers:
  default: [claude, codex]
  dev-loop: [claude, codex]
  review-loop: [claude]
"""

_PERMISSIVE = """
tool_providers:
  default: [claude, codex, opencode]
  dev-loop: [claude, codex, opencode]
"""


@pytest.fixture
def open_cwd(tmp_path, monkeypatch):
    """A cwd the linter accepts, so cwd findings never mask the policy ones."""
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    project = tmp_path / "proj"
    project.mkdir()
    return project


# ---------------------------------------------------------------------------
# The measured outage: a forced provider the policy bars
# ---------------------------------------------------------------------------

def test_forced_provider_barred_by_policy_is_an_error(
    monkeypatch, tmp_path, open_cwd, with_opencode
):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #opencode\n"

    findings = lint_queue(content)

    assert "provider_not_allowed" in _codes(findings)
    hit = next(f for f in findings if f.code == "provider_not_allowed")
    assert hit.level == LEVEL_ERROR
    assert "opencode" in hit.message
    assert "claude, codex" in hit.message
    assert exit_code_for(findings) == 2


def test_same_task_is_clean_once_the_policy_allows_the_provider(
    monkeypatch, tmp_path, open_cwd, with_opencode
):
    # The false-positive guard: identical line, widened policy, no finding.
    _install_policy(monkeypatch, tmp_path, _PERMISSIVE)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #opencode\n"

    assert "provider_not_allowed" not in _codes(lint_queue(content))


def test_unregistered_provider_is_not_reported_as_a_policy_violation(
    monkeypatch, tmp_path, open_cwd, without_opencode
):
    """The flip side of `with_opencode`, pinned so the fixture cannot quietly
    become decorative: with opencode unregistered the runtime never resolves the
    forced provider, so there is no policy verdict for the linter to predict, and
    reporting one would be a false positive.

    `opencode_missing_cli` is deliberately NOT asserted here. That check asks
    `OpencodeProvider.is_available()` (exe + opencode.json), not the dispatcher
    registry, so popping the provider does not make it fire on a box where the CLI
    really is installed — asserting it would just swap one environment dependency
    for another.
    """
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #opencode\n"

    assert "provider_not_allowed" not in _codes(lint_queue(content))


def test_tool_entry_decides_not_only_default(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Review cwd:{open_cwd} #tool:review-loop #codex\n"
    )
    findings = lint_queue(content)

    hit = next(f for f in findings if f.code == "provider_not_allowed")
    assert "review-loop" in hit.message  # scope named, not the default list


def test_model_alias_counts_as_a_forced_provider(
    monkeypatch, tmp_path, open_cwd, with_opencode
):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #opencode_glm\n"

    assert "provider_not_allowed" in _codes(lint_queue(content))


def test_allowed_provider_produces_no_finding(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #tool:dev-loop #codex\n"

    assert lint_queue(content) == []


def test_untagged_task_produces_no_finding(monkeypatch, tmp_path, open_cwd):
    # policy_dead_end() (a task with no #provider tag that policy leaves
    # unroutable) is deliberately NOT reported here — the task asked for
    # "requests a provider the policy bars".
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd}\n"

    assert lint_queue(content) == []


def test_task_tool_providers_tag_overrides_the_global_list(
    monkeypatch, tmp_path, open_cwd, with_opencode
):
    # Layer 1 of dispatcher._allowed_by_policy: the queue line's own tag wins.
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Baue X cwd:{open_cwd} #opencode #tool_providers:claude,opencode\n"
    )

    assert "provider_not_allowed" not in _codes(lint_queue(content))


def test_profile_provider_list_is_honoured(monkeypatch, tmp_path, open_cwd, with_opencode):
    """A #agent: profile whose tool_providers widens the global list must not be
    reported — the linter has to resolve the profile the way run_once() does."""
    _install_policy(monkeypatch, tmp_path, _BARRING)

    class _Profile:
        name = "wide"
        providers = ["claude", "opencode"]
        tool_providers = {"dev-loop": ["claude", "opencode"]}

    monkeypatch.setattr(queue_linter, "_resolved_profile", lambda _t: _Profile())
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #tool:dev-loop #opencode #agent:wide\n"

    assert "provider_not_allowed" not in _codes(lint_queue(content))


def test_resolved_profile_returns_none_without_an_agent_tag():
    assert queue_linter._resolved_profile("plain task #opencode") is None


# ---------------------------------------------------------------------------
# The two silently-degrading families
# ---------------------------------------------------------------------------

def test_pass_provider_barred_by_policy_is_a_warning(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Kritik cwd:{open_cwd} #tool:critical-review #pass1:claude #pass2:vibe\n"
    )
    findings = lint_queue(content)

    hit = next(f for f in findings if f.code == "pass_provider_not_allowed")
    assert hit.level == LEVEL_WARN
    assert "#pass2:vibe" in hit.message
    assert exit_code_for(findings) == 1


def test_pass1_is_never_a_policy_finding(monkeypatch, tmp_path, open_cwd):
    """`pass_providers[1]` is read nowhere in the repo — pass 1 runs on the primary
    provider under EVERY policy. Warning about it would blame the policy for a
    non-effect and imply that widening tool_providers would change something."""
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Kritik cwd:{open_cwd} #tool:critical-review #pass1:vibe #pass2:codex\n"
    )

    assert "pass_provider_not_allowed" not in _codes(lint_queue(content))


def test_pass2_on_a_tool_that_never_reads_it_is_not_a_policy_finding(
    monkeypatch, tmp_path, open_cwd
):
    """orchestrator.py hands `pass_providers` to every tool, but only
    critical-review looks at it — so `#pass2:` elsewhere degrades nothing."""
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Baue X cwd:{open_cwd} #tool:dev-loop #pass2:vibe\n"
    )

    assert "pass_provider_not_allowed" not in _codes(lint_queue(content))


def test_pass2_consumer_tool_comes_from_critical_review():
    from tools.critical_review import _TOOL_NAME

    assert queue_linter._pass2_consumer_tool() == _TOOL_NAME


def test_allowed_pass_providers_produce_no_finding(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Kritik cwd:{open_cwd} #tool:critical-review #pass1:claude #pass2:codex\n"
    )
    assert "pass_provider_not_allowed" not in _codes(lint_queue(content))


def test_second_opinion_barred_by_policy_is_a_warning(monkeypatch, tmp_path, open_cwd):
    # The documented inert case: review-loop: [claude] makes #second_opinion:codex
    # resolve to nothing, and runtime just skips the phase without a word.
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Review cwd:{open_cwd} #tool:review-loop #second_opinion:codex\n"
    )
    findings = lint_queue(content)

    hit = next(f for f in findings if f.code == "second_opinion_not_allowed")
    assert hit.level == LEVEL_WARN
    assert "codex" in hit.message


def test_second_opinion_model_alias_resolves_to_its_owner(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Review cwd:{open_cwd} #tool:review-loop #second_opinion:codex_5\n"
    )
    hit = next(f for f in lint_queue(content) if f.code == "second_opinion_not_allowed")
    assert "'codex'" in hit.message


def test_second_opinion_allowed_produces_no_finding(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Review cwd:{open_cwd} #tool:review-loop #second_opinion:claude_opus\n"
    )
    assert "second_opinion_not_allowed" not in _codes(lint_queue(content))


def test_second_opinion_unknown_alias_is_not_a_policy_finding(monkeypatch, tmp_path, open_cwd):
    # An unknown alias is _check_model_tag's business; this check must stay quiet
    # rather than blame the policy for it.
    _install_policy(monkeypatch, tmp_path, _BARRING)
    assert queue_linter._second_opinion_provider("totally_made_up") is None


def test_second_opinion_bare_provider_names_resolve():
    assert queue_linter._second_opinion_provider("vibe") == "vibe"
    assert queue_linter._second_opinion_provider("codex") == "codex"


# ---------------------------------------------------------------------------
# policy.yaml itself — missing / empty / corrupt
# ---------------------------------------------------------------------------

def test_missing_policy_file_is_a_single_warning(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, None)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd}\n"

    findings = lint_queue(content)

    assert [f.code for f in findings] == ["policy_missing"]
    assert findings[0].level == LEVEL_WARN
    assert findings[0].line_no is None
    assert exit_code_for(findings) == 1


def test_unparseable_policy_file_is_an_error(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, "tool_providers: [unclosed\n")
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd}\n"

    findings = lint_queue(content)

    assert "policy_unreadable" in _codes(findings)
    assert exit_code_for(findings) == 2


def test_non_mapping_policy_root_is_an_error(monkeypatch, tmp_path):
    _install_policy(monkeypatch, tmp_path, "- just\n- a\n- list\n")
    finding = queue_linter._policy_status()
    assert finding is not None
    assert finding.code == "policy_unreadable"
    assert finding.level == LEVEL_ERROR


def test_scalar_tool_providers_is_an_error(monkeypatch, tmp_path):
    # `dev-loop: claude` without brackets is the shape PolicyEngine warns about
    # and then ignores — silently removing the whole provider ceiling.
    _install_policy(monkeypatch, tmp_path, "tool_providers: claude\n")
    finding = queue_linter._policy_status()
    assert finding is not None
    assert finding.code == "policy_unreadable"


def test_empty_policy_file_is_a_warning_not_an_error(monkeypatch, tmp_path):
    _install_policy(monkeypatch, tmp_path, "")
    finding = queue_linter._policy_status()
    assert finding is not None
    assert finding.code == "policy_empty"
    assert finding.level == LEVEL_WARN


def test_well_formed_policy_without_tool_providers_is_not_a_finding(monkeypatch, tmp_path):
    _install_policy(monkeypatch, tmp_path, "rules:\n  - pattern: rm -rf\n    tier: deny\n")
    assert queue_linter._policy_status() is None


def test_policy_status_reads_the_engines_own_file(monkeypatch, tmp_path):
    """Linter and runtime must inspect the same path — config.VAULT_PATH is not
    consulted, so an engine built against another vault cannot be mis-reported."""
    path = _install_policy(monkeypatch, tmp_path, _BARRING)
    monkeypatch.setattr("config.VAULT_PATH", str(tmp_path / "somewhere_else"))

    assert policy_module.get_engine().config_path == path
    assert queue_linter._policy_status() is None


# ---------------------------------------------------------------------------
# Failure of the check itself must be visible, never a silent skip
# ---------------------------------------------------------------------------

def test_policy_check_failure_degrades_to_a_warning(monkeypatch, tmp_path, open_cwd):
    _install_policy(monkeypatch, tmp_path, _BARRING)
    import dispatcher

    def _boom(*a, **kw):
        raise RuntimeError("dispatcher exploded")

    monkeypatch.setattr(dispatcher, "forced_provider_policy_violation", _boom)
    content = f"## Queue\n- [ ] Baue X cwd:{open_cwd} #opencode\n"

    findings = lint_queue(content)

    assert "policy_check_failed" in _codes(findings)
    assert all(f.level != LEVEL_ERROR for f in findings if f.code == "policy_check_failed")


def test_policy_status_failure_degrades_to_a_warning(monkeypatch):
    monkeypatch.setattr(policy_module, "get_engine", lambda: (_ for _ in ()).throw(OSError("nope")))
    finding = queue_linter._policy_status()
    assert finding is not None
    assert finding.code == "policy_check_failed"
    assert finding.level == LEVEL_WARN


# ---------------------------------------------------------------------------
# The three review findings of 2026-09-09 — each of these fails on the code as
# it stood before the fix, so they are regression tests, not restatements.
# ---------------------------------------------------------------------------

def test_missing_policy_warning_describes_the_fail_closed_forced_tag(
    monkeypatch, tmp_path, open_cwd,
):
    """The message must describe the runtime correctly, in both directions.

    Until 2026-09-17 a bare `#vibe` tag RAN under a missing policy (the
    forced-tag branch never consulted _allows()), and the wording said so. The
    gap is closed now, so the warning must say the tag ends terminal - and must
    not claim the capped providers are blocked, which would read as an outage.
    """
    _install_policy(monkeypatch, tmp_path, None)
    findings = lint_queue(f"## Queue\n- [ ] Baue X cwd:{open_cwd}\n")
    msg = next(f for f in findings if f.code == "policy_missing").message

    assert "provider_not_allowed" in msg
    assert "laeuft dann trotzdem" not in msg
    assert "claude/codex/opencode laufen weiter" in msg


def test_bare_uncapped_tag_under_a_missing_policy_is_reported_as_barred(
    monkeypatch, tmp_path, open_cwd, with_vibe,
):
    """Pins the runtime behaviour the warning above describes.

    This test used to pin the OPEN gap (order[0] == 'vibe'); it was flipped when
    dispatcher closed it on 2026-09-17. The linter inherits the verdict from
    forced_provider_policy_violation(), so the per-task check must now report
    an ERROR for exactly the line the runtime rejects.

    ``with_vibe`` (tests/conftest.py) is load-bearing: with vibe unregistered,
    resolve_forced_provider() returns None and no violation can be reported.
    """
    import dispatcher

    real_allowed_by_policy = dispatcher._allowed_by_policy
    monkeypatch.setattr(dispatcher, "_allowed_by_policy", lambda *a, **kw: None)

    order, allowed = dispatcher._selection_order("do X #vibe", None, None, False, None)

    assert allowed is None
    assert order == []
    violation = dispatcher.forced_provider_policy_violation("do X #vibe")
    assert violation is not None and violation[0] == "vibe"
    assert "vibe" not in violation[1]
    assert dispatcher.policy_allows_provider("vibe", None) is False

    # Linter level, with a real missing policy.yaml instead of the patch.
    monkeypatch.setattr(dispatcher, "_allowed_by_policy", real_allowed_by_policy)
    _install_policy(monkeypatch, tmp_path, None)
    findings = lint_queue(f"## Queue\n- [ ] Baue X cwd:{open_cwd} #vibe\n")
    errors = [f for f in findings if f.code == "provider_not_allowed"]
    assert errors and errors[0].level == LEVEL_ERROR


def test_second_opinion_alias_outside_review_loops_maps_is_not_a_policy_finding(
    monkeypatch, tmp_path, open_cwd,
):
    """`opencode_glm` is a real opencode alias but not one review_loop resolves.

    The phase is skipped under EVERY policy, so widening `review-loop:` would
    silence the warning without enabling anything — the exact error `#pass1:`
    is excluded for.
    """
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Review X cwd:{open_cwd} #tool:review-loop #second_opinion:opencode_glm\n"
    )

    assert "second_opinion_not_allowed" not in _codes(lint_queue(content))
    assert queue_linter._second_opinion_provider("opencode_glm") is None
    assert queue_linter._second_opinion_provider("gemini_flash") is None


def test_second_opinion_on_a_tool_that_never_reads_it_is_not_a_policy_finding(
    monkeypatch, tmp_path, open_cwd,
):
    """Only ReviewLoopTool consumes the tag; elsewhere it is inert under any policy."""
    _install_policy(monkeypatch, tmp_path, _BARRING)
    content = (
        f"## Queue\n"
        f"- [ ] Kritik cwd:{open_cwd} #tool:critical-review #second_opinion:codex\n"
    )
    assert "second_opinion_not_allowed" not in _codes(lint_queue(content))


def test_second_opinion_consumer_tool_comes_from_review_loop():
    from tools.review_loop import ReviewLoopTool

    assert queue_linter._second_opinion_consumer_tool() == ReviewLoopTool.name


def test_second_opinion_provider_cannot_drift_from_review_loops_own_mapping():
    """Drift guard: the linter's owner lookup IS review_loop's, not a wider one."""
    from tools.review_loop import second_opinion_target

    for alias in (
        "opencode_glm", "gemini_flash", "totally_made_up",
        "claude_opus", "codex", "vibe", "or_glm", "codex_5",
    ):
        target = second_opinion_target(alias)
        expected = target[0] if target else None
        assert queue_linter._second_opinion_provider(alias) == expected, alias


def test_policy_file_path_has_no_layout_literal_of_its_own(monkeypatch, tmp_path):
    """Both forms derive from config, so there is one written-down layout.

    The default branch returns config.POLICY_FILE itself (doctor.py's path), the
    parametrised one appends config.POLICY_FILE_RELATIVE — the part queue_linter
    and every test-built engine actually need.
    """
    import config

    assert policy_module.policy_file_path() == config.POLICY_FILE
    assert config.POLICY_FILE == config.VAULT_PATH / config.POLICY_FILE_RELATIVE
    assert (
        policy_module.policy_file_path(tmp_path)
        == tmp_path / config.POLICY_FILE_RELATIVE
    )

    # And the engine keeps reading the file it was built against.
    engine = policy_module.PolicyEngine(tmp_path)
    assert engine.config_path == tmp_path / config.POLICY_FILE_RELATIVE
