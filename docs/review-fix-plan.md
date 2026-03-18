# Plan: Alle Review-Kritikpunkte beheben

## Context

Umfassende Code-Review hat 50+ Probleme identifiziert: echte Bugs, Security-Schwachstellen, Thread-Safety-Issues, systematisches Exception-Swallowing, fehlende Tests und Architektur-Schwächen. 16 Tests schlagen aktuell fehl. Ziel: Alle Findings beheben, 0 Failures, keine Regressions.

**Baseline:** 445 Tests (429 pass, 16 fail)

---

## Phase 1: Kritische Bugs (P0)

### 1.1 Set-to-List Random Element (2 Stellen)

`seen_signatures` ist ein `set` — `list(set)[-1]` gibt ein zufälliges Element.

**`tools/review_loop.py`:**
- Nach Zeile ~130 (wo `seen_signatures` deklariert wird): `last_findings_tuple: tuple[str, ...] = ()` hinzufügen
- Zeile ~166 (nach `seen_signatures.add(signature)`): `last_findings_tuple = signature`
- Zeile 232: `list(seen_signatures)[-1]` → `last_findings_tuple`

**`tools/dev_loop.py`:**
- Nach Zeile ~220 (wo `seen_quality_signatures` deklariert wird): `last_quality_tuple: tuple[str, ...] = ()` hinzufügen
- Zeile ~471 (nach `seen_quality_signatures.add(sig)`): `last_quality_tuple = sig`
- Zeile 533: `list(seen_quality_signatures)[-1]` → `last_quality_tuple`

### 1.2 16 Failing Tests fixen

**Root Cause:** Verification-Phase (review_loop) und Plan-Phase (dev_loop) machen Extra-Provider-Calls, die in Tests nicht einkalkuliert sind.

**`tests/test_review_loop.py` (3 Failures):**

| Test | Aktuell | Fix |
|------|---------|-----|
| `test_..._finishes_on_clean` | 1 Output, erwartet 1 Call | +1 Output (`"VERIFIED"`), erwartet 2 Calls |
| `test_..._fixes_p3_findings_too` | 3 Outputs, erwartet 3 Calls | +1 Output (`"VERIFIED"`), erwartet 4 Calls |
| `test_..._keeps_fixing_distinct` | 7 Outputs, erwartet 7 Calls | +1 Output (`"VERIFIED"`), erwartet 8 Calls |

**`tests/test_dev_loop.py` (13 Failures):**
- Jeder Test braucht 1 Extra-Output an Position 1 (nach Research, vor Execution) für die Plan-Phase: `"## Implementation Plan\n1. Fix the issue."`
- Prompt-Count-Assertions um 1 erhöhen
- Spezial-Provider-Klassen (`_FailExec`, `_AlwaysFailing`): Call-Counter-Logik anpassen (n==2 = Plan-Phase)
- `test_dev_loop_fails_on_research_error`: Bleibt unverändert (scheitert vor Plan-Phase)

### 1.3 Dashboard XSS

**`dashboard.py`:**
- JS-Helper im `<script>`-Block hinzufügen:
  ```javascript
  function escapeHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  ```
- Zeile 415: `ev.msg.replace(/</g, '&lt;')` → `escapeHtml(ev.msg)`
- Zeile 413: `ev.type` auch durch `escapeHtml(ev.type)` wrappen

---

## Phase 2: Security-Fixes (P1)

### 2.1 shell=True eliminieren

**`providers/claude.py`, `gemini.py`, `codex.py`:**
- Modul-Level: `_CMD = shutil.which("claude") or "claude"` (analog für gemini/codex)
- `subprocess.run()`: `shell=sys.platform == "win32"` entfernen → `shell=False`
- Fallback: Wenn `.cmd`-Datei auf Windows Probleme macht, `shell=True` nur mit sanitized Command beibehalten

### 2.2 Dashboard CORS entfernen

**`dashboard.py:479`:** `Access-Control-Allow-Origin: *` Zeile komplett entfernen (Dashboard ist localhost-only)

### 2.3 Telegram Byte-Limit

**`notifier.py`:**
- `_truncate()`: `len(text)` → `len(text.encode('utf-8'))` für Byte-Prüfung
- Zeile 127: `4096 - len(header)` → `4096 - len(header.encode('utf-8'))`
- Truncation muss an UTF-8-Byte-Grenze schneiden, dann zurück-decodieren

---

## Phase 3: Error-Handling Cleanup (P2)

### 3.1 Bare-Except → spezifische Exceptions (18 Stellen)

| Datei | Zeile(n) | Aktuell | Ersetzen durch |
|-------|----------|---------|----------------|
| `orchestrator.py` | ~599 | `except Exception:` | `except (OSError, ImportError):` |
| `dispatcher.py` | ~92, ~105 | `except Exception:` | `except (ImportError, ValueError, AttributeError):` |
| `tools/base_tool.py` | 46, 55, 62, 70 | `except Exception as exc:` | `except (ImportError, OSError) as exc:` |
| `tools/review_loop.py` | 115, 143, 240, 286 | `except Exception:` | `except (ImportError, OSError, ValueError):` |
| `tools/dev_loop.py` | 204, 235, 327, 369, 541 | `except Exception:` | `except (ImportError, OSError, ValueError):` |
| `heartbeat.py` | 75, 225 | `except Exception:` | `except (ImportError, OSError):` |
| `analytics.py` | ~315 | `except Exception:` | `except (ImportError, OSError):` + `logger.debug()` |
| `config.py` | ~315 | `except Exception:` | `except (OSError, ValueError, KeyError):` |

### 3.2 Provider-Exceptions einschränken

**`claude.py:92`, `gemini.py:68`, `codex.py:73`:**
- `except Exception as e:` → `except (OSError, ValueError) as e:`
- (Spezifische Exceptions wie `TimeoutExpired`, `FileNotFoundError` sind bereits darüber gefangen)

---

## Phase 4: Thread-Safety (P3)

### 4.1 `limits.py` — `_cache_ready` Event unter Lock

Zeilen ~977, ~982, ~1029: `_cache_ready.set()` in den `with _limits_cache_lock:`-Block verschieben.

### 4.2 `heartbeat.py` — `_queue_empty_since` Lock

- `_queue_empty_lock = threading.Lock()` hinzufügen
- Alle Lese-/Schreibzugriffe auf `_queue_empty_since` in `_check_queue_idle()` unter Lock

### 4.3 `limits.py` — Float-Drift bei 429-Estimation

Zeile ~382: `current_usage[key] = round(current_usage.get(key, 0.0) + pct, 2)`

### 4.4 `policy.py` — Event.set() unter Lock

`_respond()`: `event.set()` in den `with self._lock:`-Block verschieben

---

## Phase 5: Neue Tests (P4)

### 5.1 `tests/test_orchestrator.py` (NEU)

- `_build_prompt()`: System-Prompt + Memory + Skill + Wikilink-Assembly
- `run_once()` Happy Path mit Mock-Queue und Provider
- `run_once()` Error Path (Provider-Failure)
- Pattern: Mock `read_queue_items`, `select_provider`, `provider.run()`, `notifier.*`

### 5.2 `tests/test_dispatcher.py` (NEU)

- Fallback-Chain: Claude voll → Gemini gewählt
- Forced Provider via `#claude` Tag
- Profile-basierte Provider-Einschränkung
- Cooldown-Logik
- `strict=True` verhindert Fallback

### 5.3 `tests/test_notifier.py` (NEU)

- `_escape_markdown()` mit allen Kontrollzeichen
- `_truncate()` Byte-Awareness mit Umlauten/Emojis
- Disabled-Telegram Pfad (kein HTTP-Call)

### 5.4 `tests/test_security.py` (NEU)

- Path Traversal: `cwd:../../../etc/passwd` rejected
- Shell-Metazeichen in Task-Text
- Policy-Bypass via Case-Variation

---

## Phase 6: Architektur-Verbesserungen (P5)

### 6.1 `profiles.py:108` — String-to-List Bug

```python
# Alt:  list(v) if v else []
# Neu:
if isinstance(v, str): return [v]
return list(v) if v else []
```

### 6.2 `telegram_listener.py` — RateLimiter deque

`self._timestamps: list` → `collections.deque(maxlen=max_calls)` — verhindert unbeschränktes Wachstum.

### 6.3 `config.py` — ALLOWED_CWD_ROOTS per .env

```python
_env_roots = os.getenv("ALLOWED_CWD_ROOTS", "")
if _env_roots:
    ALLOWED_CWD_ROOTS = [Path(p.strip()) for p in _env_roots.split(";") if p.strip()]
```

### 6.4 `skills/discovery.py` — Malformed Frontmatter

Zeile 26: `content.split("---", 2)` prüfen ob `len(parts) >= 3` vor Unpacking. Sonst Fallback auf body-only SkillConfig.

### 6.5 `analytics.py` — Unbounded Log-Parsing

`_parse_log_limits()`: `MAX_CONTINUATION_LINES = 50` Guard im While-Loop.

### 6.6 `knowledge_transfer.py` — Slug-Kollisionen

Bei bestehender `output_dir`: SHA256-Hash-Suffix (6 Zeichen) anhängen.

### 6.7 `dashboard.py` — Pagination

`/api/data?days=N` Parameter, Default 7, Max 365. Cache-Key inkl. `days`.

### 6.8 Sprach-Mix in Logs

**Aufgeschoben** — rein kosmetisch, eigener PR.

---

## Phase 7: Verifikation

1. `python -m pytest tests/ -q` → 0 Failures, 460+ Tests
2. `python orchestrator.py --doctor` → alle Checks pass
3. `python orchestrator.py --dry-run` → Queue parsen ohne Fehler
4. `python orchestrator.py --dashboard` → XSS-Fix, kein CORS, Pagination
5. Git-Diff: keine unbeabsichtigten Änderungen

## Commit-Strategie

| Phase | Commit-Message |
|-------|---------------|
| 1 | `fix: critical bugs — set ordering, test failures, dashboard XSS` |
| 2 | `sec: eliminate shell=True, CORS wildcard, byte-accurate truncation` |
| 3 | `refactor: replace 18 bare except blocks with specific exception types` |
| 4 | `fix: thread safety in limits, heartbeat, policy` |
| 5 | `test: add orchestrator, dispatcher, notifier, security tests` |
| 6 | `refactor: profiles string bug, RateLimiter deque, discovery robustness` |

---

## Betroffene Dateien (Übersicht)

**Modifiziert (19):**
`tools/review_loop.py`, `tools/dev_loop.py`, `tools/knowledge_transfer.py`, `dashboard.py`, `providers/claude.py`, `providers/gemini.py`, `providers/codex.py`, `notifier.py`, `orchestrator.py`, `dispatcher.py`, `tools/base_tool.py`, `heartbeat.py`, `analytics.py`, `config.py`, `limits.py`, `policy.py`, `profiles.py`, `telegram_listener.py`, `skills/discovery.py`

**Tests modifiziert (2):**
`tests/test_review_loop.py`, `tests/test_dev_loop.py`

**Tests neu (4):**
`tests/test_orchestrator.py`, `tests/test_dispatcher.py`, `tests/test_notifier.py`, `tests/test_security.py`
