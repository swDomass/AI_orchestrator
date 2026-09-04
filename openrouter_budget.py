"""Live budget snapshot for the OpenRouter API key, via GET /api/v1/key.

Separate module instead of living in ``providers/opencode.py``, for two reasons:

1. ``limits.py`` currently imports nothing from ``providers/``, and opening that
   import direction just for this one call would be an avoidable coupling —
   ``limits.py`` stays a leaf that only reaches into small, dependency-free
   helper modules like this one.
2. The budget is a property of the **OpenRouter key**, not of the opencode CLI.
   opencode happens to route through OpenRouter (its credential file's
   ``auth.json.openrouter.key`` equals ``config.OPENROUTER_API_KEY``, measured
   2026-09-04), but the $/day cap this module reports belongs to the key, and
   would apply just as much to a hypothetical second consumer of the same key.

stdlib ``urllib`` only, no ``requests`` — same choice as ``providers/openrouter.py``.
"""

import json
import logging
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)

_BudgetTriple = tuple[float | None, float | None, str | None]
_FAIL: _BudgetTriple = (None, None, None)


def fetch_budget(timeout: float = 10.0) -> _BudgetTriple:
    """Return ``(limit, limit_remaining, limit_reset)`` from ``GET /api/v1/key``.

    JEDER Fehlerfall liefert ``(None, None, None)`` — der Aufrufer ist fail-closed.
    That includes: no API key configured, any network/timeout error, any non-2xx
    HTTP status (401 on an invalid key included), malformed/undecodable JSON, a
    missing ``data``/``limit_remaining`` field, and a structurally valid response
    where ``limit_remaining`` is JSON ``null`` (observed together with
    ``limit: null`` for a key with no configured spending cap — OpenRouter has
    nothing to report "remaining" against without a limit to subtract usage
    from). This function never raises — every branch (here and in the two
    helpers below) returns the fail triple instead.

    Measured against the production key (2026-09-04): a valid, capped key
    returns HTTP 200 with ``{"data": {"limit": 5, "limit_remaining": 4.89,
    "limit_reset": "daily", "usage_daily": 0.108, ...}}`` (note: the payload
    lives under the top-level ``"data"`` key); an invalid key returns HTTP 401.
    """
    if not config.OPENROUTER_API_KEY:
        logger.debug("openrouter_budget.fetch_budget: no OPENROUTER_API_KEY configured, skipping")
        return _FAIL

    raw = _fetch_key_endpoint(timeout)
    if raw is None:
        return _FAIL
    return _parse_budget_response(raw)


def _fetch_key_endpoint(timeout: float) -> "str | None":
    """GET /api/v1/key and return the raw response body, or None on any
    network/transport failure (never raises)."""
    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/key"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body: bytes = resp.read()
            return body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning("openrouter_budget.fetch_budget: HTTP %s from %s", e.code, url)
    except urllib.error.URLError as e:
        logger.warning("openrouter_budget.fetch_budget: unreachable (%s)", getattr(e, "reason", e))
    except TimeoutError:
        logger.warning("openrouter_budget.fetch_budget: timed out after %.0fs", timeout)
    except OSError as e:
        # Broad on purpose: urllib can surface raw socket/OSErrors beyond
        # URLError depending on platform/proxy setup, and this contract must
        # never propagate an exception to the caller (limits.py's refresh loop).
        logger.warning("openrouter_budget.fetch_budget: network error (%s)", e)
    return None


def _parse_budget_response(raw: str) -> _BudgetTriple:
    """Parse a successful GET /api/v1/key response body into the budget
    triple, or the fail triple on any malformed/incomplete shape."""
    try:
        parsed = json.loads(raw)
        key_data = parsed["data"]
        limit = key_data.get("limit")
        limit_remaining = key_data.get("limit_remaining")
        limit_reset = key_data.get("limit_reset")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        logger.warning("openrouter_budget.fetch_budget: malformed response (%s)", e)
        return _FAIL

    if limit_remaining is None:
        # Covers both "field missing entirely" and "field present but JSON
        # null" — the latter is the observed shape for an uncapped key (see
        # module docstring). Either way there is nothing to poll a budget
        # against, so this collapses to the same fail-closed triple as an
        # outright request failure rather than a partial result.
        logger.debug("openrouter_budget.fetch_budget: limit_remaining is missing/null in response")
        return _FAIL

    try:
        limit_f = float(limit) if limit is not None else None
        remaining_f = float(limit_remaining)
    except (TypeError, ValueError) as e:
        logger.warning("openrouter_budget.fetch_budget: non-numeric limit fields (%s)", e)
        return _FAIL

    reset_cadence = limit_reset if isinstance(limit_reset, str) else None
    return (limit_f, remaining_f, reset_cadence)
