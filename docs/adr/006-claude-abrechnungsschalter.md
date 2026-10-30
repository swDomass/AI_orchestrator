# ADR-006: Abrechnungsschalter API-Guthaben ↔ Abo für Claude-Läufe (`claude_billing.set_mode`)

- **Status:** accepted
- **Datum:** 2026-10-30
- **Betrifft:** AI_orchestrator / `claude_billing.py`, `config.py`, `orchestrator.py` (`--claude-billing`), `telegram_listener.py` (`/abo`, `/api`, `/status`), `doctor.py`, `dashboard.py` + `dashboard_ui/**` (`/api/claude-billing`), `tests/conftest.py`

## Kontext

ADR-005 lässt Claude-Läufe auf dem API-Guthaben laufen, sobald `ORCH_CLAUDE_API_KEY` gesetzt ist; zurück aufs Abo geht es nur automatisch (Sperre nach leerem Guthaben) oder durch Ändern der `.env` samt Neustart. Der User will zur Laufzeit zwischen Guthaben und Abo wählen, auch unterwegs (Telegram) und ohne Neustart. ADR-005 bleibt unverändert gültig; dieses ADR ergänzt eine zweite, vom User gesteuerte Ebene.

## Entscheidung

Neue Zustandsdatei `logs/claude-billing-mode.json` (`{"mode": "abo"|"api", "since", "source"}`) für die **Absicht des Users**, getrennt von der automatischen Sperre `logs/claude-api-state.json`.

- **Lesen:** `claude_billing.get_mode()` bei jedem `api_active()`-Aufruf. Fehlt die Datei → `api` (Verhalten wie vor diesem ADR); kaputt oder unbekannter Wert → `api` mit einer Warnung je Inhalt. `api_active()` ist bei `abo` False. `api_active()` bleibt die einzige Logikstelle, kein Aufrufer ändert sich.
- **Schreiben:** nur `claude_billing.set_mode(mode, source)`; unbekannter Modus → `ValueError`, Schreibfehler → `OSError` an den Aufrufer, kein Speicher-Spiegel; `api` hebt die Sperre **vor** dem Schreiben des Modus auf, lässt sich die Sperrdatei nicht löschen → `OSError`, der Modus bleibt unverändert (ein abgelaufener Rest wird ignoriert).
- **Drei Bedienwege:** CLI `python orchestrator.py --claude-billing abo|api|status`, Telegram `/abo` und `/api` (`/status` zeigt den Weg), Dashboard-Knopf (`GET`/`POST /api/claude-billing` über die bestehende Schreibschranke; steht der Schalter auf `api` und läuft eine Sperre, zeigt die Seite zusätzlich „Sperre aufheben“, damit auch dort ein Klick genügt).
- **`api` hebt eine laufende Sperre auf** (Sperrdatei und Speicher-Spiegel). Entscheidung des Users am 2026-10-30; `abo` lässt die Sperre unangetastet.
- Wirkt ab dem nächsten Claude-Lauf; `api` ohne Schlüssel bleibt Abo (`status()` meldet es).

## Alternativen

- **(a) `.env`-Variable `ORCH_CLAUDE_BILLING`** — verworfen: jede Umstellung bräuchte einen Neustart, CLI, Telegram und Dashboard könnten sie zur Laufzeit nicht ändern.
- **(b) In-Memory-Flag wie `/pause`** — verworfen: geht beim Neustart verloren, und die CLI ist ein anderer Prozess als `--watch`.
- **(c) Feld in `claude-api-state.json`** — verworfen: `clear()` löscht diese Datei nach dem ersten erfolgreichen API-Lauf, `trip()` schreibt sie neu; automatischer und manueller Zustand würden sich gegenseitig überschreiben.

## Konsequenzen

- **+** Überlebt Neustarts, aus jedem Prozess und über alle drei Bedienwege erreichbar, eine Logikstelle, Standardverhalten ohne Datei unverändert.
- **+** Absicht (User) und Zustand (automatische Sperre) bleiben getrennt lesbar; `describe()` nennt vier Fälle: API aktiv, Abo (Schalter), API gewählt ohne Schlüssel, API gewählt aber gesperrt.
- **−** Eine weitere Datei und ein kleiner Lesezugriff je `api_active()`-Aufruf.
- **−** Der manuelle `api`-Befehl übersteuert die Sperre bewusst; die Schutzwirkung der 24-h-Sperre aus ADR-005 gilt nur noch, solange der User nicht eingreift.

## Bekannte Grenzen

1. **Laufender Lauf behält seinen Weg:** der Schalter gilt ab dem nächsten Claude-Lauf.
2. **CLI und Speicher-Spiegel:** der CLI-Prozess erreicht den Speicher-Spiegel einer Sperre im Watch-Prozess nicht. Der Spiegel existiert nur nach einem gescheiterten Schreiben der Sperrdatei; Telegram und das Dashboard im Autostart unter `--watch` laufen im Watch-Prozess und löschen ihn, ein eigenständig gestartetes Dashboard (`python dashboard.py`) erreicht ihn wie die CLI nicht.
3. **`api` bei wirklich leerem Guthaben:** ein kostenloser Fehlversuch, Wiederholung auf dem Abo, erneute Telegram-Nachricht (gewollt, siehe Entscheidung).
4. **Kaputte Schalterdatei zählt als `api`:** auch eine absichtlich auf `abo` gestellte Wahl fällt bei beschädigter Datei auf `api` zurück (eine Warnung im Log).
5. **Teilzustand bei Doppelfehler:** gelingt das Löschen der Sperre und scheitert direkt danach das Schreiben des Modus, meldet der Bedienweg „nicht gespeichert", die Sperre ist aber schon weg. Harmlos: stand der Schalter schon auf `api`, ist die Absicht erreicht; stand er auf `abo`, wirkt die Sperre ohnehin nicht. Bewusst kein Rückrollen (externer Review 2026-10-30).

Verweis: ADR-005 (Abrechnungsweg, Sperre, Ledger) wird nicht umgeschrieben.
