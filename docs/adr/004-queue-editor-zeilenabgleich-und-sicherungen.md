# ADR-004: Queue-Editor mit Zeilen-Abgleich, Sicherung vor jeder Änderung und Nachfragen im eigenständigen Betrieb

- **Status:** accepted
- **Datum:** 2026-10-23
- **Betrifft:** AI_orchestrator / `queue_editor.py`, `queue_manager.py` (Wrapper), `dashboard.py`, `dashboard_ui/queue.js`, `orchestrator.py` (eine Zeile)

## Kontext

Das Dashboard soll die Queue (`agent-queue.md`) bearbeiten können. Dieselbe Datei schreiben aber
gleichzeitig der Orchestrator (Stempel, Wiederholungsmarker), Telegram-Befehle und Obsidian
(ohne Lock, Überschreiben möglich). Eine Änderung aus dem Browser darf keine fremde Änderung
überschreiben und nicht die falsche Zeile treffen. Der Server weiß außerdem nur dann, welcher Task
läuft, wenn er im Orchestrator-Prozess läuft. Bei `python dashboard.py` weiß er es nicht.

## Entscheidung

- Neues, HTTP-freies Modul `queue_editor.py` über `queue_manager`: parsen, klassifizieren, Zeilen
  bauen, prüfen, mutieren, sichern, rückgängig machen. `dashboard.py` bekommt nur dünne Routen
  (`/api/queue*`, hinter der Schreibschranke aus ADR-003).
- **Zeilen-Abgleich statt Datei-Hash:** jede Aktion nennt die Zeile als `ref = {lines, occurrence,
  occurrences}`. Steht genau dieser Block unter dem Lock nicht mehr so da → 409 `changed`; ändert sich
  die Anzahl identischer Blöcke → 409 `ambiguous`. Nur die Rohtext-Ansicht gleicht die ganze Datei
  per SHA256 ab.
- **Ein Lock-Durchgang** über den neuen öffentlichen Wrapper `queue_manager.apply_queue_update`
  (zusammen mit `read_queue_text` und `queue_file`), plus ein **zweiter Byte-Vergleich** unmittelbar
  vor der Rückgabe, weil Obsidian ohne Lock schreibt.
- **Sicherung vor jeder Änderung:** Paar `logs/queue-backups/<Zeit>-<aktion>.{md,json}`
  (`ORCH_QUEUE_BACKUP_DIR`, die neuesten 20 je Queue-Datei). Scheitert die Sicherung, wird nichts
  geändert (503). „Rückgängig" gibt es eine Stufe: die neueste Sicherung, solange die Datei noch dem
  Stand nach dieser Änderung entspricht.
- **Laufender Task:** im Orchestrator-Prozess reicht `start_autostart(in_flight=...)` einen Getter
  durch (`orchestrator.py`: `in_flight=lambda: _in_flight_task`, eine nur lesende Zeile); die Zeile ist
  gesperrt (423). Eigenständig fragt der Editor bei Aktionen an offenen Zeilen und bei raw/undo nach
  (428 „Laufstatus unbekannt"); ein laufender Tool-Run aus `logs/active_runs/` sperrt in beiden Modi.
- **Gefährliche Tags** (`#shutdown`, `#approve:`, `#tool_providers:`) werden erkannt und nur nach
  ausdrücklicher Bestätigung (`confirm_dangerous`) geschrieben.
- Keine Statusdatei des Orchestrators.

## Alternativen

- **(a) Datei-SHA für jede Aktion** — verworfen: jede Hintergrund-Änderung (Orchestrator, Telegram,
  Obsidian) würde jede Bearbeitung blockieren, auch an ganz anderen Zeilen.
- **(b) Nur die Zeilennummer** — verworfen: Einfügungen verschieben sie, die Aktion träfe die falsche
  Zeile.
- **(c) Eigene Lock-Schleife im Editor** — verworfen: Drift gegenüber dem Locking in `queue_manager`.
- **(d) In-flight-Datei des Orchestrators** — verworfen: Eingriff in das Absturz-Register des
  Orchestrators für eine Anzeige; als Folgepunkt in der ROADMAP offen.
- **(e) Im eigenständigen Betrieb nur lesen** — verworfen: der Reiter wäre dort nutzlos. Gewählt ist
  Nachfragen mit Bestätigung.

## Konsequenzen

- **+** 409 kommt nur, wenn genau die betroffene Zeile anders ist; Telegram, Orchestrator und Obsidian
  stören an anderen Zeilen nicht.
- **+** Die Sicherungen machen ein Überschreib-Rennen mit Obsidian reparierbar.
- **+** Eine Fehlerquelle weniger: ein Lock-Durchgang über dieselbe Mechanik wie der Orchestrator.
- **−** Bei identischen Zeilen, zu denen gleichzeitig eine weitere identische Zeile kommt, gibt es
  konservativ `ambiguous`.
- **−** Die Sicherungen enthalten Task-Texte lokal in `logs/` und reichen nur 20 Stufen zurück;
  Rückgängig ist eine Stufe.
- **−** Im eigenständigen Betrieb ist eine Bestätigung nötig; das Dashboard rät nie „läuft nicht".
- **−** Der Getter muss das Modul lesen, in dem `_set_in_flight` läuft: `run_orchestrator.ps1` startet
  `orchestrator.py` als Skript, andere Module importieren eine zweite Kopie (eigener Test).
