import subprocess
from unittest.mock import patch

import config
import dispatcher
import doctor


def test_check_claude_cli_does_not_fake_auth_verification():
    base_result = doctor.CheckResult(doctor.PASS, "Claude CLI", "claude 1.2.3")

    with (
        patch.object(doctor, "_check_cli", return_value=base_result),
        patch.object(doctor.subprocess, "run", side_effect=AssertionError("unexpected subprocess call")),
    ):
        result = doctor.check_claude_cli()

    assert result.status == doctor.PASS
    assert result.label == "Claude CLI"
    assert "auth not verified" in result.message.lower()


# ── check_model_aliases ───────────────────────────────────────────────────────

def test_check_model_aliases_pass_when_all_alive():
    with patch("heartbeat._probe_model", return_value=(True, "")):
        r = doctor.check_model_aliases()
    assert r.status == doctor.PASS
    assert "verified" in r.message.lower()


def test_check_model_aliases_fail_on_dead_id():
    def fake_probe(provider, model_id, **kw):
        if model_id.startswith("claude-haiku"):
            return False, "model not found"
        return True, ""

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    assert r.status == doctor.FAIL
    assert "dead" in r.message.lower()
    assert "claude-haiku" in r.message
    assert "MODEL_ALIASES" in r.fix_hint


def test_check_model_aliases_warn_on_transient():
    def fake_probe(provider, model_id, **kw):
        return True, "transient (rate_limit)"

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    assert r.status == doctor.WARN
    assert "unverified" in r.message


def test_check_model_aliases_handles_probe_exceptions():
    """Per-probe exceptions must not crash the doctor — they count as transient (alive=True)."""
    def fake_probe(provider, model_id, **kw):
        raise RuntimeError("boom")

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    # Exceptions wrapped as transient → since detail starts with "transient", warn
    assert r.status == doctor.WARN


# ── check_opencode_cli ──────────────────────────────────────────────────────
#
# All five findings are independently WARN, never FAIL (opencode is optional,
# same posture as check_vibe_cli()). opencode.json is a tmp-fixture dict here —
# NEVER the real file, per the auftrag. _resolve_exe()/_load_opencode_config()
# are patched at their source module (providers.opencode) so the function's own
# lazy `from providers.opencode import ...` picks up the patched version.


_FAKE_EXE = "C:/fake/opencode.exe"


def _valid_opencode_cfg():
    """opencode.json shape that satisfies all five checks — mutate a deep copy
    per test to break exactly one of them."""
    from config import OPENCODE_MODEL_ALIASES

    models = {}
    for full_id in OPENCODE_MODEL_ALIASES.values():
        _, _, model_key = full_id.partition("/")
        models[model_key] = {
            "id": "some/model-id",
            "name": "Some Model",
            "options": {"provider": {"data_collection": "deny", "zdr": True}},
        }
    # small_model points at a REAL ZDR alias from the map above, not just at
    # something with the right prefix: since 2026-09-04 the check runs it through
    # the same contract as the tag aliases, because the small-model path is the
    # documented ZDR bypass (9 measured runs went to Google / opencode Zen direct).
    first_alias_key = next(iter(models))
    return {
        "agent": {"extern-review": {}, "extern-dev": {}},
        "provider": {"openrouter": {"models": models}},
        "small_model": f"openrouter/{first_alias_key}",
    }


def _patch_opencode_check(cfg, *, exe=_FAKE_EXE, budget=(5.0, 4.89, "daily"), which=None):
    return (
        patch("providers.opencode._resolve_exe", return_value=exe),
        patch("providers.opencode._load_opencode_config", return_value=cfg),
        patch("openrouter_budget.fetch_budget", return_value=budget),
        patch.object(doctor.shutil, "which", return_value=which),
    )


def test_check_opencode_cli_pass_when_everything_is_set_up():
    p1, p2, p3, p4 = _patch_opencode_check(_valid_opencode_cfg())
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.PASS
    assert r.label == "opencode CLI"


def test_check_opencode_cli_warns_on_shim_only_exe():
    """1. Only the npm .CMD/.ps1 shim resolvable, no real .exe next to it."""
    p1, p2, p3, p4 = _patch_opencode_check(
        _valid_opencode_cfg(), exe=None, which="C:/npm/opencode.CMD",
    )
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "shim" in r.message.lower()
    assert "opencode.exe" in r.message


def test_check_opencode_cli_warns_when_opencode_not_on_path_at_all():
    p1, p2, p3, p4 = _patch_opencode_check(_valid_opencode_cfg(), exe=None, which=None)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "path" in r.message.lower()


def test_check_opencode_cli_warns_on_missing_agents():
    """2. extern-review/extern-dev missing from opencode.json's agent map."""
    cfg = _valid_opencode_cfg()
    cfg["agent"] = {"extern-review": {}}  # extern-dev missing
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "extern-dev" in r.message


def test_check_opencode_cli_warns_on_missing_zdr_alias():
    """3a. A handpicked alias is entirely absent from opencode.json."""
    cfg = _valid_opencode_cfg()
    del cfg["provider"]["openrouter"]["models"]["zdr-review"]
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "opencode_deepseek" in r.message
    assert "fehlt" in r.message.lower()


def test_check_opencode_cli_warns_on_alias_missing_zdr_flags():
    """3b. Alias exists but without data_collection:deny + zdr:true — the ZDR
    restriction hangs on the alias, not the raw model id (measured 2026-09-04)."""
    cfg = _valid_opencode_cfg()
    cfg["provider"]["openrouter"]["models"]["zdr-review"]["options"] = {
        "provider": {"data_collection": "allow", "zdr": False}
    }
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "data_collection" in r.message or "zdr" in r.message.lower()


def test_check_opencode_cli_handles_non_dict_provider_entry():
    """Fix 1: a hand-mangled opencode.json where provider.openrouter is a LIST
    (not a dict) must not crash the whole check via AttributeError — the old
    code's `providers_cfg.get(provider_name, {})` only guards a MISSING key,
    not a present-but-wrong-shaped one, so `.get("models")` on the list blew up
    before this fix. Same posture as a missing entry: reported, not raised."""
    cfg = _valid_opencode_cfg()
    cfg["provider"]["openrouter"] = ["not", "a", "dict"]
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "fehlt in opencode.json" in r.message


def test_check_opencode_cli_warns_on_default_model_not_in_config():
    """Fix 2: config.OPENCODE_DEFAULT_MODEL is what every #opencode task
    WITHOUT an alias tag actually resolves to — an unresolvable target there
    does not error, it hangs (measured 2026-09-04). Must be checked like the
    three tag aliases, not left to fail silently at runtime."""
    cfg = _valid_opencode_cfg()
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with (
        p1, p2, p3, p4,
        patch.object(config, "OPENCODE_DEFAULT_MODEL", "openrouter/does-not-exist-default"),
    ):
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "OPENCODE_DEFAULT_MODEL" in r.message
    assert "fehlt" in r.message.lower()


def test_check_opencode_cli_no_extra_finding_for_valid_default_model():
    """Normal case: OPENCODE_DEFAULT_MODEL resolves to an entry carrying the
    full ZDR contract but is NOT one of the three OPENCODE_MODEL_ALIASES
    values — exercises the actual _zdr_contract_violation() call for the
    default (not the dedup skip against an identical alias target) and must
    not add a finding on top of the baseline PASS."""
    cfg = _valid_opencode_cfg()
    cfg["provider"]["openrouter"]["models"]["custom-default"] = {
        "id": "some/model-id",
        "options": {"provider": {"data_collection": "deny", "zdr": True}},
    }
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with (
        p1, p2, p3, p4,
        patch.object(config, "OPENCODE_DEFAULT_MODEL", "openrouter/custom-default"),
    ):
        r = doctor.check_opencode_cli()
    assert r.status == doctor.PASS
    assert "Standardmodell" not in r.message


def test_check_opencode_cli_no_duplicate_finding_when_default_matches_alias():
    """No Doppel-Befund: today OPENCODE_DEFAULT_MODEL == OPENCODE_MODEL_ALIASES
    ["opencode_deepseek"] == "openrouter/zdr-review" — the general form is
    "default byte-identical to an already-checked alias target". Reproduced
    here via a distinct pair (zdr-review-alt / opencode_glm, explicitly
    patched as the default) so the assertion does not depend on which alias
    happens to share the default's value today, and so cfg's own small_model
    (which independently points at "openrouter/zdr-review") cannot smuggle in
    a second occurrence and hide a dedup regression. Breaking the one shared
    entry must produce ONE finding (the alias one), never a second near-
    duplicate "Standardmodell" finding for the identical full_id."""
    cfg = _valid_opencode_cfg()
    cfg["provider"]["openrouter"]["models"]["zdr-review-alt"]["options"] = {
        "provider": {"data_collection": "allow", "zdr": False}
    }
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with (
        p1, p2, p3, p4,
        patch.object(config, "OPENCODE_DEFAULT_MODEL", "openrouter/zdr-review-alt"),
    ):
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "Standardmodell" not in r.message
    assert r.message.count("zdr-review-alt") == 1


def test_check_opencode_cli_warns_on_missing_small_model():
    """4. Top-level small_model unset — the small-model path can bypass both
    the $ cap and ZDR (measured: 4x Google, 5x opencode Zen)."""
    cfg = _valid_opencode_cfg()
    del cfg["small_model"]
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "small_model" in r.message


def test_check_opencode_cli_warns_on_small_model_without_the_zdr_contract():
    """The gap the prefix check left open (found by the opencode reviewer).

    `openrouter/<something>` routes through the capped key, so the money side is
    fine — but it says nothing about data protection, and the small model is
    precisely the documented ZDR bypass. A target that is missing from the models
    map, or present without `data_collection: deny` + `zdr: true`, used to PASS.
    """
    cfg = _valid_opencode_cfg()
    cfg["provider"]["openrouter"]["models"]["kein-zdr"] = {
        "id": "some/model-id",
        "options": {"provider": {}},          # prefix ok, contract missing
    }
    cfg["small_model"] = "openrouter/kein-zdr"
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "small_model" in r.message
    assert "zdr" in r.message.lower()


def test_check_opencode_cli_warns_on_small_model_alias_that_does_not_exist():
    """Same check, other half: the prefix is right but the alias is not in the
    config at all — opencode would fail to resolve it, and the doctor said PASS."""
    cfg = _valid_opencode_cfg()
    cfg["small_model"] = "openrouter/gibt-es-nicht"
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "small_model" in r.message


def test_check_opencode_cli_warns_on_small_model_not_openrouter():
    cfg = _valid_opencode_cfg()
    cfg["small_model"] = "opencode/zen"
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "small_model" in r.message


def test_check_opencode_cli_warns_when_key_has_no_cap():
    """5. openrouter_budget.fetch_budget() returns the fail triple — without a
    cap AllLimits.opencode is permanently available=False."""
    p1, p2, p3, p4 = _patch_opencode_check(_valid_opencode_cfg(), budget=(None, None, None))
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert "available=False" in r.message or "deckel" in r.message.lower()


def test_check_opencode_cli_never_fails():
    """opencode is optional — every one of the five findings is a WARN, never a
    FAIL, even when ALL of them fire at once."""
    cfg = {}  # missing agents, missing provider aliases, missing small_model
    p1, p2, p3, p4 = _patch_opencode_check(cfg, exe=None, which=None, budget=(None, None, None))
    with p1, p2, p3, p4:
        r = doctor.check_opencode_cli()
    assert r.status == doctor.WARN
    assert r.status != doctor.FAIL


def test_check_opencode_cli_never_reads_the_real_config_file(tmp_path):
    """Sanity: this test suite must never touch the real opencode.json — the
    patched _load_opencode_config() return value is a plain dict, not a path."""
    cfg = _valid_opencode_cfg()
    p1, p2, p3, p4 = _patch_opencode_check(cfg)
    with p1, p2, p3, p4:
        # config.OPENCODE_CONFIG_PATH is never consulted because
        # _load_opencode_config itself is patched out entirely.
        r = doctor.check_opencode_cli()
    assert r.status == doctor.PASS


# ── check_policy_file: --fix copies policy.example.yaml ──────────────────────

def _policy_env(tmp_path, monkeypatch):
    """Point the policy check at tmp_path; returns the (not yet existing) target."""
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "99_System" / "AI" / "policy.yaml"
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "POLICY_FILE", target)
    return target


def test_policy_example_constant_points_at_the_shipped_file():
    assert doctor._POLICY_EXAMPLE.is_file()
    assert doctor._POLICY_EXAMPLE.name == "policy.example.yaml"


def test_policy_fix_copies_the_example_byte_for_byte_and_check_then_passes(tmp_path, monkeypatch):
    target = _policy_env(tmp_path, monkeypatch)

    before = doctor.check_policy_file()
    assert before.status == doctor.WARN
    assert before.fix_fn is not None
    before.fix_fn()

    assert target.read_bytes() == doctor._POLICY_EXAMPLE.read_bytes()
    after = doctor.check_policy_file()
    assert after.status == doctor.PASS
    assert "12 tool_providers entries" in after.message  # 11 tools + "default"


def test_policy_fix_never_overwrites_a_file_that_appeared_after_the_check(tmp_path, monkeypatch, capsys):
    target = _policy_env(tmp_path, monkeypatch)
    result = doctor.check_policy_file()          # file absent here
    target.parent.mkdir(parents=True)
    target.write_text("auto:\n  - \"my own rule\"\n", encoding="utf-8")

    result.fix_fn()

    assert target.read_text(encoding="utf-8") == "auto:\n  - \"my own rule\"\n"
    assert "already exists" in capsys.readouterr().out


def test_policy_check_leaves_an_existing_file_alone(tmp_path, monkeypatch):
    target = _policy_env(tmp_path, monkeypatch)
    target.parent.mkdir(parents=True)
    target.write_text("deny:\n  - \"x\"\n", encoding="utf-8")

    r = doctor.check_policy_file()

    assert r.fix_fn is None
    assert target.read_text(encoding="utf-8") == "deny:\n  - \"x\"\n"


def test_policy_example_missing_creates_nothing_and_offers_no_fix(tmp_path, monkeypatch):
    target = _policy_env(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "_POLICY_EXAMPLE", tmp_path / "does-not-exist.yaml")

    r = doctor.check_policy_file()

    assert r.status == doctor.WARN
    assert r.fix_fn is None
    assert not target.parent.exists()
    assert "policy.example.yaml" in r.message or "example" in r.message


# ── Provider chain: CLI checks follow dispatcher._PRIORITY ────────────────────

def _only_on_path(monkeypatch, *present, chain=("claude", "codex")):
    """Pretend only the named binaries exist; never spawn a real process."""
    monkeypatch.setattr(doctor.shutil, "which", lambda c: f"/bin/{c}" if c in present else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no spawn")))
    monkeypatch.setattr(doctor, "_dispatcher_chain", lambda: tuple(chain))


def test_claude_install_hint_names_the_right_package(monkeypatch):
    _only_on_path(monkeypatch)
    r = doctor.check_claude_cli()
    assert r.status == doctor.FAIL
    assert "npm install -g @anthropic-ai/claude-code" in r.fix_hint
    assert "@anthropic/" not in r.fix_hint


def test_missing_gemini_is_warn_without_install_advice_when_not_in_chain(monkeypatch):
    _only_on_path(monkeypatch, "claude")
    r = doctor.check_gemini_cli()
    assert r.status == doctor.WARN
    assert "not in dispatcher._PRIORITY" in r.message
    assert "npm" not in r.fix_hint and "npm" not in r.message


def test_missing_gemini_stays_fail_when_it_is_in_the_chain(monkeypatch):
    _only_on_path(monkeypatch, chain=("claude", "gemini"))
    r = doctor.check_gemini_cli()
    assert r.status == doctor.FAIL
    assert "npm install -g @google/gemini-cli" in r.fix_hint


def test_missing_node_is_warn_not_fail(monkeypatch):
    _only_on_path(monkeypatch)
    r = doctor.check_node()
    assert r.status == doctor.WARN
    assert "native Claude CLI needs no Node" in r.message


def test_chain_rule_only_claude_present_makes_codex_a_warn(monkeypatch):
    _only_on_path(monkeypatch, "claude")
    results = [doctor.check_claude_cli(), doctor.check_gemini_cli(), doctor.check_codex_cli()]
    assert results[2].status == doctor.FAIL  # raw check unchanged
    doctor._apply_chain_rule(results)
    assert [r.status for r in results] == [doctor.PASS, doctor.WARN, doctor.WARN]


def test_chain_rule_all_chain_members_missing_is_fail(monkeypatch):
    _only_on_path(monkeypatch)
    results = [doctor.check_claude_cli(), doctor.check_gemini_cli(), doctor.check_codex_cli()]
    doctor._apply_chain_rule(results)
    assert [r.status for r in results] == [doctor.FAIL, doctor.WARN, doctor.FAIL]


def test_chain_rule_ignores_a_pass_outside_the_chain(monkeypatch):
    """gemini installed but not in the chain must not rescue a chain without claude/codex."""
    _only_on_path(monkeypatch, "gemini")
    results = [doctor.check_claude_cli(), doctor.check_gemini_cli(), doctor.check_codex_cli()]
    doctor._apply_chain_rule(results)
    assert results[0].status == doctor.FAIL and results[2].status == doctor.FAIL


def test_startup_checks_do_not_mention_gemini_outside_the_chain(monkeypatch, capsys):
    _only_on_path(monkeypatch)
    ok = doctor.CheckResult(doctor.PASS, "x", "ok")
    monkeypatch.setattr(doctor, "check_vault_path", lambda: ok)
    monkeypatch.setattr(doctor, "check_queue_file", lambda: ok)
    monkeypatch.setattr("notifier.notify_error", lambda *a, **k: None)
    assert doctor.run_startup_checks() is False
    out = capsys.readouterr().out
    assert "claude, codex" in out
    assert "gemini" not in out.lower()


def test_startup_checks_pass_with_only_claude(monkeypatch, capsys):
    _only_on_path(monkeypatch, "claude")
    ok = doctor.CheckResult(doctor.PASS, "x", "ok")
    monkeypatch.setattr(doctor, "check_vault_path", lambda: ok)
    monkeypatch.setattr(doctor, "check_queue_file", lambda: ok)
    assert doctor.run_startup_checks() is True
    assert "Providers available: claude" in capsys.readouterr().out


_STUBBED_CHECKS = (
    "check_opencode_cli", "check_git", "check_cclimits", "check_vault_path",
    "check_queue_file", "check_telegram_bot", "check_env_file", "check_skills",
    "check_memory_dir", "check_heartbeat_file", "check_profiles",
    "check_policy_file", "check_model_aliases", "check_worktrees",
)


def test_run_doctor_end_to_end_with_only_claude_is_ok(monkeypatch, capsys):
    _only_on_path(monkeypatch, "claude")
    ok = doctor.CheckResult(doctor.PASS, "stub", "ok")
    for name in _STUBBED_CHECKS:
        monkeypatch.setattr(doctor, name, lambda: ok)
    assert doctor.run_doctor(fix=False) is True
    out = capsys.readouterr().out
    assert "Result: OK" in out
    assert "Result: FAIL" not in out


def test_check_model_aliases_skips_gemini_probes_outside_the_chain(monkeypatch):
    probed = []
    monkeypatch.setattr(doctor, "_dispatcher_chain", lambda: ("claude", "codex"))
    monkeypatch.setattr("heartbeat._probe_model", lambda p, m, **kw: probed.append(p) or (True, ""))
    assert doctor.check_model_aliases().status == doctor.PASS
    assert probed and "gemini" not in probed


def test_check_model_aliases_probes_gemini_when_it_is_in_the_chain(monkeypatch):
    probed = []
    monkeypatch.setattr(doctor, "_dispatcher_chain", lambda: ("claude", "gemini", "codex"))
    monkeypatch.setattr("heartbeat._probe_model", lambda p, m, **kw: probed.append(p) or (True, ""))
    doctor.check_model_aliases()
    assert "gemini" in probed


def test_dispatcher_chain_reads_dispatcher_priority():
    assert doctor._dispatcher_chain() == tuple(dispatcher._PRIORITY)


def test_run_doctor_with_only_codex_still_fails_on_missing_claude(monkeypatch, capsys):
    """Claude is required (test-loop and the safety hook are Claude-only): never downgraded."""
    _only_on_path(monkeypatch, "codex")
    ok = doctor.CheckResult(doctor.PASS, "stub", "ok")
    for name in _STUBBED_CHECKS:
        monkeypatch.setattr(doctor, name, lambda: ok)
    results = [doctor.check_claude_cli(), doctor.check_codex_cli()]
    doctor._apply_chain_rule(results)
    assert [r.status for r in results] == [doctor.FAIL, doctor.PASS]
    assert doctor.run_doctor(fix=False) is False
    assert "Result: FAIL" in capsys.readouterr().out


def test_chain_rule_keeps_a_broken_codex_install_as_fail(monkeypatch):
    """Only 'not found in PATH' is downgraded; any other FAIL keeps its status and text."""
    _only_on_path(monkeypatch, "claude")
    results = [
        doctor.check_claude_cli(),
        doctor.CheckResult(doctor.FAIL, "Codex CLI", "exec format error"),
    ]
    doctor._apply_chain_rule(results)
    assert results[1].status == doctor.FAIL
    assert results[1].message == "exec format error"


def test_gemini_non_missing_problem_keeps_its_real_message(monkeypatch):
    _only_on_path(monkeypatch, "claude")
    monkeypatch.setattr(
        doctor, "_check_cli",
        lambda *a, **k: doctor.CheckResult(doctor.FAIL, "Gemini CLI", "exec format error"),
    )
    r = doctor.check_gemini_cli()
    assert r.status == doctor.WARN  # Gemini is not in the chain
    assert "exec format error" in r.message
    assert "not installed" not in r.message


def _cclimits_env(monkeypatch, *, installed, api_key="", returncode=0):
    """cclimits present/absent on PATH; the (patched) run returns ``returncode``."""
    monkeypatch.setattr(doctor.shutil, "which", lambda c: "/bin/cclimits" if installed else None)
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout="", stderr="boom"),
    )
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", api_key)


def test_cclimits_not_installed_without_api_key_is_fail(monkeypatch):
    _cclimits_env(monkeypatch, installed=False)
    r = doctor.check_cclimits()
    assert r.status == doctor.FAIL
    assert "npm install -g cclimits" in r.fix_hint


def test_cclimits_not_installed_with_api_key_is_warn(monkeypatch):
    _cclimits_env(monkeypatch, installed=False, api_key="sk-test")
    r = doctor.check_cclimits()
    assert r.status == doctor.WARN
    assert "Codex still needs cclimits" in r.message


def test_cclimits_installed_but_run_fails_is_warn(monkeypatch):
    _cclimits_env(monkeypatch, installed=True, returncode=1)
    r = doctor.check_cclimits()
    assert r.status == doctor.WARN
    assert "exited 1" in r.message


def test_run_doctor_with_claude_but_no_cclimits_fails(monkeypatch, capsys):
    _only_on_path(monkeypatch, "claude")
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", "")
    ok = doctor.CheckResult(doctor.PASS, "stub", "ok")
    for name in _STUBBED_CHECKS:
        if name != "check_cclimits":
            monkeypatch.setattr(doctor, name, lambda: ok)
    assert doctor.run_doctor(fix=False) is False
    assert "Result: FAIL" in capsys.readouterr().out
