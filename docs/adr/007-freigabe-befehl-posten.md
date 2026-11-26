# ADR-007: Telegram-Freigabebefehl `/posten_<Kennung>` startet einen fest konfigurierten Subprozess

- **Status:** accepted
- **Datum:** 2026-11-26
- **Betrifft:** AI_orchestrator / `telegram_listener.py` (`_cmd_posten`, `_run_posten`), `config.py` (`FREIGABE_POSTEN_*`), `.env.example`, `tests/test_telegram_freigabe.py`; Gegenstelle: WL-SocialMedia (Plan `.dev-loop/post-upload-2026-11-26/`, E1)

## Kontext

WL-SocialMedia baut ein Post-Paket und schickt eine Telegram-Vorschau mit einer 12-stelligen Hex-Kennung. Nach Durchsicht soll ein Fingertipp am Handy den Upload auslösen. Der Upload ist irreversibel (öffentliche Posts). Der Orchestrator besitzt den Telegram-Bot und den Listener, soll aber keinen WL-Code kennen.

## Entscheidung

Der Listener erkennt den Befehl `/posten_<12 Hex>[_<google|facebook|youtube|pinterest|instagram>]` (nach Chat-ID-Prüfung und Befehlslimit) und startet in einem Daemon-Thread genau einen Subprozess aus drei `.env`-Werten (`ORCH_FREIGABE_POSTEN_PYTHON`, `_BEFEHL`, `_CWD`): Argumentliste, `shell=False`, Timeout 1800 s, höchstens ein Lauf gleichzeitig (nicht blockierende `threading.Lock`). Der Subprozess läuft mit `-X utf8` und `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` (der Bericht enthält Emoji; unter Windows wäre das Kind sonst cp1252 und stürbe an der Ausgabe). Der Listener quittiert (seit Runde 4 erst nach dem Start des Worker-Threads) und meldet danach nur unerwartete Ausgänge (Exit außer 0/3/4/5, Timeout, Startfehler); die Ergebnis-Codes meldet der Posten-Prozess selbst: 0 ok, 3 abgelehnt, 4 unklar, **5 mindestens ein Fehler gemeldet**. Exit 1 ist ein Absturz (Traceback) und kommt mit dem Ende von stderr als Telegram-Meldung vom Listener. Fehlen die Werte oder ein Pfad, antwortet er „nicht eingerichtet“ und startet nichts. `/approve`, Policy und Queue bleiben unberührt. **Der Safety-Hook** (`SAFETY_DENY_PATTERNS`, Doppel in `scripts/safety_hook.py`) sperrt seit 2026-11-26 (Runde 2, Entscheid des Users) für Agenten und Sitzungen jeden Shell-Befehl, der das Posten-Werkzeug (`wl_socialmedia.posten` oder `posten/befehl.py`) mit dem Wort `freigabe` und ohne `--probelauf` aufruft; der Listener startet den Befehl direkt und läuft nicht durch den Hook. Damit kann ein Queue-Agent, der die Kennung in einer Vorschau sieht, nicht selbst freigeben. Grenzen: eine Schutzmaßnahme gegen Versehen, nicht gegen einen Angreifer (`--probelauf` irgendwo im selben Befehl, Start über Variable oder `cd` + `python befehl.py freigabe`, Aufruf aus Python umgehen sie). Die Sperre im WL-Prozess (Sperrdatei je Kennung) bleibt die zweite Linie.
- **Interpreter:** `ORCH_FREIGABE_POSTEN_PYTHON` muss der Basis-Interpreter sein, nicht das venv-Startprogramm `Scripts\python.exe`. Dieses ist nur ein Starter und startet den echten Interpreter als Kindprozess; beim Timeout beendet `subprocess.run` nur den Starter, der Upload liefe verwaist weiter. Hinweis in `.env.example`.

## Alternativen

- **(a) Queue-Zeile, ein KI-Agent führt aus** — verworfen: ein nicht-deterministischer Agent stünde zwischen Freigabe und irreversibler Handlung; der Weg hängt an Kontingent und Anbieter-Verfügbarkeit, der Agent könnte „hilfreich“ nachbessern oder wiederholen.
- **(b) Listener schreibt nur eine Freigabedatei, eine geplante Windows-Aufgabe holt sie ab** — verworfen: eine zweite Daueranlage zu pflegen, verzögerte Rückmeldung, zwei Ausfallstellen.
- **(c) `/approve` der Policy wiederverwenden** — verworfen: `/approve` gilt der einen ausstehenden Policy-Anfrage und trägt keine Kennung; ein Paket-Fingerabdruck ließe sich damit nicht binden.

## Konsequenzen

- **+** Zwischen Freigabe und Veröffentlichung steht kein Modell; die Kennung (aus dem Paketinhalt abgeleitet) bindet jede Freigabe an einen Zustand. Der Orchestrator kennt nur Präfix, Kennungsformat und drei Werte.
- **−** Der Orchestrator startet fremden Code. Eingrenzung: feste Befehlszeile aus `.env`, strenge Kennungs-Regex, `shell=False`, nur der konfigurierte Chat.
- **−** Freigaben bei abgeschaltetem Listener gehen verloren (kein Puffer, der Rückstau wird beim Start verworfen). Ein Neustart des Orchestrators während eines Laufs lässt den Kindprozess verwaist weiterlaufen; die Sperre im WL-Prozess (eigene Sperrdatei) fängt einen zweiten Lauf ab.
- **−** Die Single-Flight-Sperre gilt nur im Listener-Prozess und ist nicht persistent.
- **−** Ob Telegram `/posten_<Kennung>` in einer Bot-Nachricht antippbar macht, ist live zu prüfen; Rückfall: lange drücken, kopieren, senden.
- **−** Das Sperrmuster im Sicherheits-Filter (`config.py`, Ersatzliste `scripts/safety_hook.py`) blockiert auch harmlose Befehle, in denen der Modulpfad `wl_socialmedia.posten` (bzw. `posten/befehl.py`) und das Wort „freigabe“ vorkommen, etwa eine Commit-Nachricht zu diesem Thema oder `pytest --cov=wl_socialmedia.posten -k freigabe`. Reine Reibung, kein Sicherheitsproblem; Ausweg: Formulierung ändern. Umgekehrt ist es nur Schutz gegen Versehen (z. B. `--probelauf` irgendwo im selben Befehl umgeht es).
- **−** **Nur direkt getippte Befehle (Runde 4):** `/posten…` mit Weiterleitungs- oder `via_bot`-Merkmal wird abgelehnt („Freigabe nur direkt tippen, nicht weiterleiten“), kein Prozess. Das Ende von stderr geht geschwärzt (Token-Muster → `<geschwärzt>`) an Telegram; die drei `ORCH_FREIGABE_POSTEN_*`-Werte müssen absolute Pfade sein (relativ → „nicht eingerichtet“) und werden aufgelöst genutzt.
- **−** **Quittung nach dem Thread-Start:** „Upload startet“ kommt erst, wenn der Worker-Thread läuft; scheitert der Start, wird die Sperre freigegeben und der Fehler gemeldet.
- **−** **Regeltext für Nicht-Claude-Anbieter:** der in Prompts eingefügte Sicherheitstext (`config.py`) und die Liste gesperrter Kategorien (`CLAUDE.md`) nennen `posten … freigabe` ohne `--probelauf`. Das ist eine **weiche Regel** (Anweisung an ein Modell), keine Sperre; hart wirkt nur der Safety-Hook, und dieser nur für Claude-Sitzungen.
- **−** **Bekannte Grenzen:** die Zustellung des Tor-Berichts an Telegram ist unbestätigt (fällt Telegram aus, fehlt auch die Ersatzmeldung des Listeners); der Timeout beendet nur das direkte Kind, keinen Prozessbaum (Unterprozesse wie SFTP oder ffmpeg können weiterlaufen).
