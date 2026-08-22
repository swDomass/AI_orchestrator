"""Tamper pin must cover files a verify script only dispatches to.

Regression: the two vault-gardener wrappers carry no logic of their own. Pinning only the
file named in the `#verify:` tag left the deciding file free — a provider could rewrite it
to `exit 0` during its run, the untouched wrapper would execute it, and the orchestrator
would run provider-authored code outside the sandbox. Found by the external (Mistral)
review pass; the internal round had rated the same observation as marginal.

The tests produce the violation instead of asserting the guard exists.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config._load_dotenv"):
    import orchestrator


WRAPPER = """<#
.SYNOPSIS
    Wrapper.

# verify-depends: logic.ps1
#>
& (Join-Path $PSScriptRoot 'logic.ps1')
exit $LASTEXITCODE
"""


@pytest.fixture()
def scripts(tmp_path):
    (tmp_path / "wrapper.ps1").write_text(WRAPPER, encoding="utf-8")
    (tmp_path / "logic.ps1").write_text("exit 0\n", encoding="utf-8")
    return tmp_path


def _pin(scripts):
    return orchestrator._pin_verify_script(
        f"task #verify:wrapper.ps1 cwd:{scripts}", str(scripts)
    )


def test_dependency_is_pinned(scripts):
    pin = _pin(scripts)
    assert pin.digest is not None
    assert len(pin.deps) == 1
    dep_path, dep_digest = pin.deps[0]
    assert Path(dep_path).name == "logic.ps1"
    assert dep_digest is not None


def test_tampered_dependency_is_refused(scripts):
    """The actual attack: wrapper untouched, logic swapped for an unconditional pass."""
    pin = _pin(scripts)
    (scripts / "logic.ps1").write_text("exit 0\n# harmlos aussehend\n", encoding="utf-8")

    passed, detail = orchestrator._run_verify_script("wrapper.ps1", str(scripts), pin=pin)

    assert not passed
    assert "logic.ps1" in detail


def test_deleted_dependency_is_refused(scripts):
    """A vanished dependency is not "nothing to check", it is an unverifiable state."""
    pin = _pin(scripts)
    (scripts / "logic.ps1").unlink()

    passed, detail = orchestrator._run_verify_script("wrapper.ps1", str(scripts), pin=pin)

    assert not passed


def test_untouched_dependency_passes(scripts):
    """Counter-probe — without it the matrix only proves the check can say no."""
    pin = _pin(scripts)

    passed, detail = orchestrator._run_verify_script("wrapper.ps1", str(scripts), pin=pin)

    assert passed, detail


def test_declaration_cannot_point_outside_the_script_directory(scripts):
    """A path in the declaration would let a compromised script aim the pin elsewhere."""
    (scripts / "wrapper.ps1").write_text(
        WRAPPER.replace("verify-depends: logic.ps1", "verify-depends: ../../elsewhere.ps1"),
        encoding="utf-8",
    )

    pin = _pin(scripts)

    assert pin.deps == ()


def test_declaration_pointing_at_a_missing_file_is_refused(scripts):
    """A stale declaration must not silently disable the whole gate.

    `_digest_file` returns None for a missing file, so `None != None` waved the check
    through — and the file that actually runs stayed unpinned. Rename the logic file,
    forget to update the declaration, and the pin is off with no signal. Reproduced:
    the real logic file was then tampered with and the check still reported "passed".
    """
    (scripts / "wrapper.ps1").write_text(
        WRAPPER.replace("verify-depends: logic.ps1", "verify-depends: logic_OLD.ps1"),
        encoding="utf-8",
    )
    pin = orchestrator._pin_verify_script(
        f"task #verify:wrapper.ps1 cwd:{scripts}", str(scripts)
    )
    assert pin.deps and pin.deps[0][1] is None, "Vorbedingung: Digest konnte nicht gebildet werden"

    (scripts / "logic.ps1").write_text('Write-Output "immer gruen"\nexit 0\n', encoding="utf-8")
    passed, detail = orchestrator._run_verify_script("wrapper.ps1", str(scripts), pin=pin)

    assert not passed
    assert "logic_OLD.ps1" in detail


def test_script_without_declaration_has_no_deps(scripts):
    (scripts / "wrapper.ps1").write_text("exit 0\n", encoding="utf-8")

    pin = _pin(scripts)

    assert pin.deps == ()
    assert pin.digest is not None
