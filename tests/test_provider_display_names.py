"""Tests for limits.all_provider_names() / display_provider_names() and the four
status-display sites that consume them.

Background: heartbeat.py (capacity log + heartbeat text) and telegram_listener.py
(/status + /limits) each carried a hand-written ("claude", "gemini", "codex")
tuple. That was wrong in both directions — opencode became an AllLimits field on
2026-09-04 and was never displayed, while gemini left every active path on
2026-08-15 and was displayed on every poll. All four sites now derive the list —
but not the same one: the three human-facing messages take the policy-filtered
display_provider_names(), the capacity log takes the unfiltered
all_provider_names() because it is a data recorder analytics parses later.
"""

import dataclasses
import threading
from unittest.mock import patch

import heartbeat
import limits
import telegram_listener
from limits import AllLimits, ProviderLimits, all_provider_names, display_provider_names

# ---------------------------------------------------------------------------
# The derivation itself
# ---------------------------------------------------------------------------

def test_all_provider_names_matches_dataclass_fields():
    assert all_provider_names() == tuple(f.name for f in dataclasses.fields(AllLimits))


def test_all_provider_names_includes_opencode():
    # The concrete regression: opencode joined AllLimits on 2026-09-04 and no
    # status site ever mentioned it.
    assert "opencode" in all_provider_names()


def test_display_provider_names_keeps_only_policy_allowed():
    with patch("dispatcher.policy_allows_provider", side_effect=lambda n, t: n != "gemini"):
        assert display_provider_names() == ("claude", "codex", "opencode")


def test_display_provider_names_drops_a_retired_provider_but_keeps_opencode():
    # The live policy.yaml shape as measured 2026-09-09: default:
    # [claude, codex, opencode] — gemini out, opencode in.
    allowed = {"claude", "codex", "opencode"}
    with patch("dispatcher.policy_allows_provider", side_effect=lambda n, t: n in allowed):
        names = display_provider_names()
    assert "opencode" in names
    assert "gemini" not in names


def test_display_provider_names_asks_the_default_entry_not_a_tool():
    seen = []

    def _spy(name, tool_name):
        seen.append((name, tool_name))
        return True

    with patch("dispatcher.policy_allows_provider", side_effect=_spy):
        display_provider_names()
    assert {t for _n, t in seen} == {None}


def test_display_provider_names_fails_open_when_policy_lookup_raises():
    # A broken policy layer must not blank the status display at 03:00.
    with patch("dispatcher.policy_allows_provider", side_effect=RuntimeError("boom")):
        assert display_provider_names() == all_provider_names()


def test_display_provider_names_never_returns_empty():
    # A pathological policy that bars everything still yields a usable display.
    with patch("dispatcher.policy_allows_provider", return_value=False):
        assert display_provider_names() == all_provider_names()


# ---------------------------------------------------------------------------
# Drift guard: the four sites must go through the helper, not a literal tuple.
# Patching the helper to a sentinel makes a reverted hand-written list fail.
# ---------------------------------------------------------------------------

_SENTINEL = ("codex", "opencode")


def _limits_with(*names) -> AllLimits:
    return AllLimits(**{n: ProviderLimits(available=True, remaining_pct=42.0) for n in names})


def test_capacity_log_enumerates_exactly_the_derived_names(tmp_path, monkeypatch):
    log = tmp_path / "capacity-log.md"
    monkeypatch.setattr(heartbeat, "CAPACITY_LOG_FILE", log)
    monkeypatch.setattr(heartbeat, "all_provider_names", lambda: _SENTINEL)

    heartbeat._append_capacity_log(_limits_with(*all_provider_names()))

    content = log.read_text(encoding="utf-8")
    logged = {ln.split("|")[1].strip() for ln in content.splitlines() if "|" in ln and "<!--" not in ln}
    assert logged == set(_SENTINEL), content


def test_capacity_log_is_a_recorder_and_is_not_filtered_by_policy(tmp_path, monkeypatch):
    """The recorder must log every AllLimits field, including ones policy bars.

    logs/capacity-log.md is parsed by analytics._parse_capacity_log() for the
    dashboard's current-limits panel and its historical series. Filtering it by
    the policy's ``default:`` entry ends a provider's history the moment that
    entry narrows — even while the provider keeps executing tasks under a
    per-tool allow-list. Failure case pinned here: policy allows claude only,
    yet codex/gemini/opencode rows must still be recorded.
    """
    log = tmp_path / "capacity-log.md"
    monkeypatch.setattr(heartbeat, "CAPACITY_LOG_FILE", log)

    with patch("dispatcher.policy_allows_provider", side_effect=lambda n, t: n == "claude"):
        assert display_provider_names() == ("claude",)
        heartbeat._append_capacity_log(_limits_with(*all_provider_names()))

    content = log.read_text(encoding="utf-8")
    logged = {ln.split("|")[1].strip() for ln in content.splitlines() if "|" in ln and "<!--" not in ln}
    assert logged == set(all_provider_names()), content


def test_check_limits_enumerates_exactly_the_derived_names(monkeypatch):
    monkeypatch.setattr(heartbeat, "display_provider_names", lambda: _SENTINEL)
    monkeypatch.setattr(heartbeat, "_append_capacity_log", lambda _l: None)

    out = heartbeat._check_limits(lambda: _limits_with(*all_provider_names()))

    assert out is not None
    for name in _SENTINEL:
        assert f"{name}: " in out
    for name in set(all_provider_names()) - set(_SENTINEL):
        assert f"{name}: " not in out


def test_cmd_status_enumerates_exactly_the_derived_names(monkeypatch):
    monkeypatch.setattr(telegram_listener, "display_provider_names", lambda: _SENTINEL)
    monkeypatch.setattr(telegram_listener, "read_queue", lambda: [])
    monkeypatch.setattr(telegram_listener, "get_limits", lambda: _limits_with(*all_provider_names()))
    sent: list[str] = []
    monkeypatch.setattr(telegram_listener, "send_message", lambda m, **kw: sent.append(m))

    telegram_listener.TelegramListener(threading.Event())._cmd_status()

    assert len(sent) == 1
    for name in _SENTINEL:
        assert f"  {name}: " in sent[0]
    for name in set(all_provider_names()) - set(_SENTINEL):
        assert f"  {name}: " not in sent[0]


def test_cmd_limits_enumerates_exactly_the_derived_names(monkeypatch):
    monkeypatch.setattr(telegram_listener, "display_provider_names", lambda: _SENTINEL)
    monkeypatch.setattr(telegram_listener, "get_limits", lambda: _limits_with(*all_provider_names()))
    sent: list[str] = []
    monkeypatch.setattr(telegram_listener, "send_message", lambda m, **kw: sent.append(m))

    telegram_listener.TelegramListener(threading.Event())._cmd_limits()

    assert len(sent) == 1
    for name in _SENTINEL:
        assert f"*{name}*: " in sent[0]
    for name in set(all_provider_names()) - set(_SENTINEL):
        assert f"*{name}*: " not in sent[0]


def test_no_status_site_hardcodes_the_provider_tuple():
    """Belt-and-braces: the literal itself must be gone from both modules."""
    import inspect
    for module in (heartbeat, telegram_listener):
        src = inspect.getsource(module)
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue  # explanatory comments may quote the old literal
            assert '("claude", "gemini", "codex")' not in line, (
                f"{module.__name__} still hand-enumerates providers: {line!r}"
            )


def test_limits_module_keeps_its_intentional_hand_written_lists():
    """limits.py's own cclimits-probe enumerations are deliberate (opencode is
    not a cclimits provider) — this fix must not have touched them."""
    import inspect
    src = inspect.getsource(limits)
    assert '("claude", "gemini", "codex")' in src
