# ADR-002: Task-Text steht zuletzt im Prompt + Post-Task-Outcome-Check

- **Status:** accepted
- **Datum:** 2026-07-25
- **Betrifft:** AI_orchestrator / `orchestrator._build_prompt`, `queue_manager`, `providers/process_runner.py`
- **Löst ab:** [ADR-001](001-claude-stdin-file-delivery.md) (Ursachenanalyse dort widerlegt)

## Kontext

Der tägliche `morning-brief`-Task fiel am 20., 24. und 25.07.2026 still aus (dazwischen
ein gesunder Lauf am 21.07.): `success: true`, exit 0, gültiges `result`-Event — und eine
Modell-Antwort der Form „ich sehe deine Konfiguration, aber keine konkrete Aufgabe". Drei
aufeinanderfolgende Fixes am stdin-Transport blieben wirkungslos.

**Gemessen statt vermutet.** Claude Code legt pro Lauf eine Session-Datei unter
`~/.claude/projects/<projekt>/*.jsonl` ab; deren erste `type=="user"`-Message ist der
tatsächlich angekommene Prompt. Vergleich Ausfall gegen Erfolg:

| Lauf | Länge | Task-Text enthalten | Task-Start | danach Kontext |
|---|---|---|---|---|
| Ausfall 25.07. | 22.770 | ja | 14.082 (62 %) | 8.688 Zeichen |
| Ausfall 24.07. | 22.026 | ja | 14.083 (64 %) | 7.943 Zeichen |
| Erfolg 21.07. | 22.888 | ja | 14.059 (61 %) | 8.829 Zeichen |

Erfolg und Ausfall sind strukturell derselbe Prompt. **Es gab nie ein
Zustellungsproblem.** Die Ursache lag im Aufbau: `queue_manager.inject_file_context()`
lieferte `task + blocks`, und `_build_prompt` hängte das als letzten Block an. Die
Aufgabe stand dadurch mitten im Prompt, gefolgt von ~8.700 Zeichen Dateiinhalten; der
Prompt endete mit fremder Konfigurationsdokumentation. Eine Aufgaben-Sektion existierte
nicht (Probes auf `## Aufgabe`/`## Task` in allen drei Prompts negativ).

Verschärfend: Der Kommentar in `process_runner._feed_stdin` behauptete das Gegenteil
(„the task text sits at the very END"). Diese ungeprüfte Aussage war die Grundlage aller
drei Fehlversuche.

## Entscheidung

1. **Der Task steht immer zuletzt.** `collect_file_context()` liefert nur die
   Kontextblöcke; `_build_prompt` hängt den Task als letzten Block unter `## Aufgabe`
   an. Reihenfolge: `core → skills → memory → Dateikontext → ## Aufgabe`. Der alte
   Wrapper `inject_file_context()` wurde mit seinem letzten Aufrufer entfernt.
2. **Ergebnis prüfen statt dem Lauf glauben** — neues Queue-Tag `#verify:<script>`. Der
   Check inspiziert das erwartete Artefakt (steht der Briefing-Block wirklich in der
   Daily-Note?) und schlägt damit unabhängig von der Ursache an, auch bei künftigen.
   Er läuft in allen drei Erfolgspfaden nach der Finalisierung, fail-closed, mit
   Hash-Pinning gegen Manipulation durch den Provider und über den tree-killenden
   Watchdog.
3. **Das Detektionsnetz misst wieder**, statt Zustellung zu behaupten:
   `_verify_prompt_file` vergleicht die Bytes auf Platte gegen die Nutzlast.

## Alternativen

- **Verifikation innerhalb der SKILL.md** — verworfen: Im Fehlerfall erreicht die
  Aufgabe das Modell nicht, der Skill läuft also gar nicht. Der Check wäre blind für
  genau den Fall, für den er existiert.
- **Heuristische Fingerabdruck-Erkennung am Antworttext** („keine konkrete Aufgabe") —
  verworfen: rät am Modelltext statt am Ergebnis, falsch-positiv bei legitimen
  Rückfragen.
- **Task bei Verify-Fehlschlag requeuen** — verworfen: bräuchte einen eigenen
  begrenzten Zähler (wie der Hang-Pfad), sonst requeut ein kaputtes Check-Skript einen
  funktionierenden Task endlos. Der Task wird finalisiert, aber die Erfolgsmeldung
  unterdrückt und als `verify_failed` verbucht.

## Konsequenzen

- **+** Die Instruktion steht an der Stelle, an der sie nicht mehr übersehen werden
  kann; der variable Teil wandert ans Ende, was dem Prompt-Cache eher nützt.
- **+** Mit `#verify:` gibt es erstmals einen Wächter, der nicht von einer Theorie über
  die Ursache abhängt — er prüft, ob die Arbeit passiert ist.
- **−** Der Orchestrator führt benutzerdefinierte Skripte aus; der Blast Radius wächst.
  Abgesichert durch Hash-Pinning vor dem Provider-Lauf, fail-closed bei jedem Zweifel
  und Ausführung über den tree-killenden Watchdog.
- **−** Ein fehlerhaftes Check-Skript erzeugt Fehlalarme. Bewusst in Kauf genommen: ein
  Fehlalarm ist sichtbar, ein stiller Ausfall nicht.

## Lehre

Die Prompt-Struktur wurde drei Runden lang aus einem Code-Kommentar abgeleitet statt
einmal gemessen. Die Session-Dateien, die den tatsächlich angekommenen Prompt enthalten,
lagen die ganze Zeit vor. Wer die Reihenfolge in `_build_prompt` ändert, korrigiert
bitte auch die Aussagen darüber in `process_runner` — und leitet Prompt-Struktur nie aus
einem Kommentar ab.
