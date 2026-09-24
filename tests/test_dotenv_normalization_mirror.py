"""P3-2 (oc r1): ``tests/conftest.py``'s ``_find_real_vault_path`` cannot import
``config`` (it must read ``ORCH_VAULT_PATH`` from ``.env`` BEFORE the first
``import config`` anywhere in the process, or the redirect it exists to set up
never takes effect — see that function's docstring). So conftest.py carries its
own copy of ``config._normalize_dotenv_value`` (config.py:7) instead of
importing it. A copy can silently drift from its source; this test is the
guard against that, holding the two functions equal across a battery of
example values. `config` MAY be imported here (this file is not conftest.py,
so importing it does not disturb the vault-path redirect timing).
"""
from __future__ import annotations

import conftest

import config

EXAMPLE_VALUES = [
    # Bare, no quotes, no comment.
    "D:/vault",
    # Trailing whitespace only.
    "D:/vault   ",
    # Double-quoted.
    '"D:/vault"',
    # Single-quoted.
    "'D:/vault'",
    # Double-quoted with an inline comment AFTER the closing quote.
    '"D:/vault" # sync dir',
    # Single-quoted with an inline comment after the closing quote.
    "'D:/vault' # sync dir",
    # Unquoted with an inline comment (whitespace before '#').
    "D:/vault # sync dir",
    # Unquoted, no whitespace before '#' — must NOT be treated as a comment
    # (config._normalize_dotenv_value requires whitespace before '#' so a URL
    # fragment like https://x.com#anchor is not truncated).
    "https://x.com#anchor",
    # '#' INSIDE quotes must survive — it's part of the value, not a comment.
    '"D:/vault#literal"',
    "'D:/vault#literal'",
    # Quoted value containing a '#' followed by more text, still inside quotes.
    '"D:/vau#lt/with/hash"',
    # Empty value.
    "",
    # Only whitespace.
    "   ",
    # Windows path with backslashes, quoted, plus inline comment.
    '"D:\\OneDrive - White Lady e.U\\Notizbuecher\\obsidian\\cerebrum_v2" # sync dir',
    # Windows path with backslashes, unquoted, plus inline comment.
    "D:\\OneDrive - White Lady e.U\\Notizbuecher\\obsidian\\cerebrum_v2 # sync dir",
]


def test_conftest_mirror_matches_config_normalize_dotenv_value():
    for raw in EXAMPLE_VALUES:
        expected = config._normalize_dotenv_value(raw)
        actual = conftest._normalize_dotenv_value(raw)
        assert actual == expected, (
            f"mirror diverged for {raw!r}: conftest gave {actual!r}, "
            f"config gave {expected!r}"
        )
