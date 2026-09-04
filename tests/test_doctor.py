from unittest.mock import patch

import config
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
