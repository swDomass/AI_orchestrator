# Lint- und Typecheck-Baseline — 2026-09-02

Erstmalige Vermessung des Repos mit `ruff` und `mypy`. Dieser Bericht ist eine
**Bestandsaufnahme, keine Umsetzung**: ausser `pyproject.toml` und dieser Datei
wurde nichts angefasst, insbesondere kein `ruff --fix` und kein `ruff format`.

Gemessen wurde auf dem Arbeitsbaum vom 2026-09-02 abends, also **inklusive der zu
dem Zeitpunkt noch uncommitteten Aenderungen an `queue_manager.py` und
`queue_linter.py`** aus demselben Abend - nicht gegen `HEAD`. Der Effekt ist klein
(rund 24 Befunde in `queue_manager.py`), aber die Zahlen unten sind damit keine
reine HEAD-Baseline.

## Kernbefund vorweg

> **Das Repo laesst sich auf Python 3.10–3.13 nicht importieren.** `limits.py:85`
> benutzt `ProviderLimits` in einer Modul-Annotation 19 Zeilen bevor die Klasse
> definiert wird. Auf Python 3.14 faellt das dank PEP 649 (verzoegerte
> Annotations-Auswertung) nicht auf — auf jeder aelteren Version ist es ein
> `NameError` beim Import. `README.md:64` nennt als Anforderung `Python 3.10+`.
> Details und Beleg unter [D1](#d1--limitspy85--f821-undefined-name).

Der Rest des Berichts ist Routine: 1351 ruff-Befunde und 131 mypy-Fehler, davon
der grosse Teil Stil, Mechanik oder bewusste Architektur.

## Setup

| | |
|---|---|
| ruff | `0.16.1` |
| mypy | `2.3.1` (+ `types-PyYAML`) |
| Interpreter der Messung | CPython `3.14.2` (win32) |
| Konfiguration | `pyproject.toml` (neu, in diesem Schritt angelegt) |
| Dateien im Scope | 176 `.py` (davon 92 Testdateien) |

Nachstellen:

```bash
pip install "ruff==0.16.1" "mypy==2.3.1" types-PyYAML
ruff check . --statistics
ruff check . --output-format=concise
ruff format --check .            # NIE ohne --check — wuerde 156 Dateien umschreiben
python -m mypy .
```

Weder `ruff` noch `mypy` sind in `requirements*.txt` gepinnt. Die Zahlen unten
sind nur zusammen mit den obigen Versionen aussagekraeftig. Ein
`requirements-dev.txt` waere der saubere Ort dafuer — bewusst **nicht** angelegt,
weil der Auftrag den Diff auf Konfiguration + Bericht begrenzt (offene Frage
Nr. 1 am Ende).

### Woher die Regelauswahl kommt

Uebernommen von den Schwester-Repos statt neu erfunden:

| | `eeg_analysis` | `pyrolyse-1d-model` | **hier** |
|---|---|---|---|
| Ablage | ruff+mypy in `pyproject.toml` | ruff in `ruff.toml`, mypy in `pyproject.toml` | wie eeg_analysis |
| `line-length` / `target-version` | 100 / py312 | 100 / py312 | **100 / py312** |
| ruff `select` | `E,W,F,I,UP,B,SIM` | `+C4,N,RUF,PL,NPY` | **Vereinigung minus `NPY`** |
| mypy | lenient | `strict = true` | **lenient (eeg-Linie)** |

Vier bewusste Abweichungen, jede auch im `pyproject.toml` kommentiert:

1. **`NPY` raus** — keine numpy-Abhaengigkeit im Repo, die Regelgruppe waere tot.
2. **`N802/N803/N806/N815` NICHT uebernommen.** pyrolyse ignoriert sie
   ausschliesslich wegen seiner SI-Suffix-Konvention (`temperature_K`,
   `pressure_Pa`, dortiges CLAUDE.md §7.2). Dieses Repo hat keine solche
   Konvention — ein Copy-Paste haette **11** echte Naming-Befunde stumm
   geschaltet. Gemessen: `ruff check . --ignore N802,N803,N806,N815` → 1340
   gegen 1351. Von den vier Regeln feuern hier nur zwei, `N806` (6) und `N802`
   (5); `N803` und `N815` haben null Treffer. Die verbleibenden 5 Naming-Befunde
   der Baseline sind `N814` (camelcase-imported-as-constant) — die Regel steht in
   `pyrolyse-1d-model/ruff.toml:25-28` gerade **nicht** in der Ignore-Liste und
   waere von einem Copy-Paste also gar nicht betroffen gewesen.
3. **`per-file-ignores` weggelassen** — waere hier tote Konfiguration:
   `PLR2004` ist bereits global ignoriert, `S` (bandit) und `T` (print) sind
   gar nicht selektiert.
4. **mypy lenient statt strict.** Gemessen, nicht geraten: `--strict` liefert auf
   diesem Code **3851** Fehler in 147 Dateien gegen **131** lenient — Faktor 29,
   bei ~850 von 3463 `def`s mit Rueckgabe-Annotation. Eine 3851-Zeilen-Baseline
   ist kein Arbeitsmittel. `strict` steht unten als Ziel, nicht als Startpunkt.

Zwei mypy-Flags sind reine Layout-Notwendigkeit und in keinem der beiden
Schwester-Repos noetig (die haben ein `src/`- bzw. `model/`-Package):
`explicit_package_bases` + `namespace_packages`. Ohne sie bricht mypy sofort ab
mit `Source file found twice under different module names: "build_audit_pack"
and "scripts.build_audit_pack"` — `scripts/`, `tests/` und `hooks/` haben kein
`__init__.py`, die Root-Module sind top-level. Wenn spaeter ein
`scripts/__init__.py` auftaucht, verschiebt sich diese Zuordnung erneut.

### Warum py312, obwohl die Messung auf 3.14 lief

Beide Schwester-Repos stehen auf py312, und py314 aendert das Bild kaum: +21
Befunde (ausschliesslich `UP`-Regeln), mypy identisch 131. py312 ist die
Untergrenze, die der Code erfuellen *sollte* — und genau diese Einstellung hat
[D1](#d1--limitspy85--f821-undefined-name) sichtbar gemacht.

## Ruff — 1351 Befunde

Aufteilung: **723 Produktivcode**, **628 Tests**.

Spalte *Kat.*: **D** = Defekt · **M** = mechanisch (echt, aber risikolos) ·
**S** = Stil · **A** = Architektur-Entscheidung noetig · **FP** = False Positive

| n | Regel | Kat. | Anmerkung |
|---:|---|:--:|---|
| 524 | `PLC0415` import-outside-top-level | **A** | 199 src / 325 Tests. Trifft das bewusste Lazy-Import-Muster (Zirkelimport-Vermeidung). Siehe [E1](#e1--plc0415-524). |
| 123 | `I001` unsorted-imports | S | autofixbar |
| 109 | `F401` unused-import | **M** | 29 src / 80 Tests. Stichprobe `analytics.py:24` (`QUEUE_FILE`) verifiziert: kommt genau einmal vor, naemlich im Import. Echt. |
| 48 | `UP045` non-pep604-annotation-optional | S | `Optional[X]` → `X \| None`, autofixbar |
| 43 | `PLW0603` global-statement | **A** | alle 43 im src. Singleton-Muster (`PolicyEngine`, `UsageSuggester`, Provider-Caches) — bauartbedingt, kein Defekt. |
| 41 | `W293` blank-line-with-whitespace | S | Formatter-Sache |
| 34 | `PLR0912` too-many-branches | S | Komplexitaets-Signal, keine Aktion |
| 34 | `PLW1510` subprocess-run-without-check | **A** | meist bewusst (`rc` wird selbst geprueft), aber pro Stelle zu bestaetigen |
| 34 | `UP037` quoted-annotation | S | autofixbar |
| 27 | `PLR0911` too-many-return-statements | S | |
| 26 | `PLR0917` too-many-positional-arguments | S | |
| 26 | `SIM105` suppressible-exception | S | `contextlib.suppress` statt `try/except/pass` |
| 25 | `F541` f-string-missing-placeholders | **M** | autofixbar |
| 24 | `UP017` datetime-timezone-utc | S | `datetime.UTC` |
| 24 | `UP035` deprecated-import | S | `typing.Dict` etc. |
| 20 | `RUF059` unused-unpacked-variable | **M** | |
| 19 | `PLW0108` unnecessary-lambda | S | |
| 16 | `B010` set-attr-with-constant | S | |
| 15 | `PLW2901` redefined-loop-name | S | oft absichtliches Normalisieren |
| 12 | `F841` unused-variable | **D/M** | 4 src / 8 Tests. Zwei davon riechen nach halb verdrahtetem Feature → [D5](#d5--f841-tote-werte-in-dev_loop-und-review_loop). |
| 9 | `E702` multiple-statements-on-one-line | S | |
| 8 | `RUF100` unused-noqa | **FP/M** | Gemischt: 5× `non-enabled: BLE001` (FP, muessen bleiben), 3× `unused: E402` (echt tot, entfernbar). Siehe [E3](#e3--ruf100-8). |
| 8 | `W291` trailing-whitespace | S | |
| 7 | `B905` zip-without-explicit-strict | **D?** | verdeckt stille Laengen-Mismatches → [D4](#d4--b905-zip-ohne-strict-7) |
| 7 | `UP006` non-pep585-annotation | S | |
| 6 | `E731` lambda-assignment | S | |
| 6 | `N806` non-lowercase-variable-in-function | S | die Befunde, die pyrolyses Ignore verdeckt haette |
| 5 | `C420`, 5 `E741`, 5 `N802`, 5 `N814`, 5 `SIM117` | S | |
| 4 | `C408`, 4 `RUF015`, 4 `RUF022`, 4 `SIM103` | S | |
| 3 | `B007` unused-loop-control-variable | S | |
| 3 | `B023` function-uses-loop-variable | **D** | → [D2](#d2--b023-telegram_listenerpy942944945) |
| 3 | `RUF012` mutable-class-default | S | |
| 3 | `RUF046` unnecessary-cast-to-int | S | |
| 2 | `F811`, 2 `PLR0402`, 2 `RUF005`, 2 `SIM102`, 2 `SIM108`, 2 `SIM114` | S | |
| 1 | `C401`, 1 `E401`, 1 `E402`, 1 `PLR1730`, 1 `RUF019`, 1 `RUF023`, 1 `SIM300`, 1 `UP031` | S | |
| 1 | `F402` import-shadowed-by-loop-var | **D?** | → [D3](#d3--f402-toolsbase_toolpy138) |
| 1 | `F821` undefined-name | **D** | → [D1](#d1--limitspy85--f821-undefined-name) — der Kernbefund |
| 1 | `SIM115` open-file-with-context-handler | **FP** | → [E4](#e4--sim115-providersprocess_runnerpy608) |

483 der 1351 sind mit `--fix` autofixbar, weitere 113 nur mit `--unsafe-fixes`.

### Top-15-Dateien

| n | Datei | | n | Datei |
|---:|---|---|---:|---|
| 74 | `heartbeat.py` | | 29 | `tests/test_limits.py` |
| 52 | `orchestrator.py` | | 28 | `quota_calibration.py` |
| 50 | `tests/test_analytics.py` | | 28 | `parallel_runner.py` |
| 42 | `limits.py` | | 26 | `tests/test_tool_provider_layering.py` |
| 39 | `usage_suggester.py` | | 24 | `tests/test_heartbeat_recap.py` |
| 34 | `doctor.py` | | 24 | `queue_manager.py` |
| 33 | `telegram_listener.py` | | 23 | `skills/discovery.py` |
| 31 | `tests/test_heartbeat_model_check.py` | | | |

`heartbeat.py` fuehrt mit Abstand, ein Drittel davon `PLC0415`.

## Mypy — 131 Fehler in 36 Dateien

**115 Produktivcode**, **16 Tests** (von 176 geprueften Dateien). Die 63
`annotation-unchecked`-Zeilen sind *notes*, keine Fehler, und hier nicht gezaehlt.

| n | Code | Kat. | Anmerkung |
|---:|---|:--:|---|
| 41 | `arg-type` | S | ueberwiegend fehlende Annotationen, nicht falsche Aufrufe |
| 28 | `attr-defined` | S | |
| 18 | `no-any-return` | S | Folge von `warn_return_any` |
| 11 | `union-attr` | **D?** | 4× `process_runner.py:347-365` (`proc.stdin`), je 1× `policy.py:120`, `orchestrator.py:1343/35`, 4× in Tests → [D6](#d6--union-attr-auf-optional-11) |
| 9 | `assignment` | **M** | 4 der 9 sind dasselbe Muster: optionaler Import, im `except` auf `None` gesetzt (`heartbeat.py:1067`, `tools/base_tool.py:528`, `tools/review_loop.py:457`, `tools/dev_loop.py:330`) → [E6](#e6--optionale-imports-ohne-annotation-heartbeatpy10671082-und-3-weitere) |
| 6 | `operator` | S | |
| 5 | `unused-ignore` | **A** | **Keine Leichen** — alle 5 sind Artefakte der lenient-Konfiguration und werden wieder scharf, sobald Paket 12 greift → [E7](#e7--unused-ignore-5--artefakte-der-lenient-konfiguration) |
| 3 | `var-annotated` | S | |
| 2 | `override` | S | `review_loop.py:362`, `tests/test_base_provider.py:28` |
| 2 | `index` | **FP** | `limits.py:1261` verifiziert FP, `notifier.py:117` offen |
| 2 | `dict-item` | S | |
| 2 | `call-overload` | S | `notifier.py:176`, `analytics.py:339` |
| 1 | `truthy-function` | **M** | `heartbeat.py:1082` — **kein** toter Guard; nur zusammen mit `assignment` `:1067` lesbar → [E6](#e6--optionale-imports-ohne-annotation-heartbeatpy10671082-und-3-weitere) |
| 1 | `no-redef` | **M** | `orchestrator.py:1789` |

Top-Dateien: `tools/dev_loop.py` 15, `providers/process_runner.py` 15,
`tools/review_loop.py` 11, `tools/pr_babysitter.py` 8, `orchestrator.py` 7,
`notifier.py` 7, `limits.py` 7, `ci_watcher.py` 6.

## Echte Defekte

Alle folgenden Stellen wurden im Quelltext nachgelesen, nicht dem Regelnamen
geglaubt.

### D1 — `limits.py:85` — `F821 undefined-name`

**Schwere: hoch. Der einzige Befund mit Ausfall-Charakter.**

```python
# limits.py:85   — ProviderLimits wird erst in Zeile 104 definiert
_429_snapshots: dict[str, tuple[ProviderLimits, float]] = {}
```

`limits.py` hat kein `from __future__ import annotations`. Auf Python ≤ 3.13
werden Modul-Annotationen sofort ausgewertet → `NameError` beim Import. Auf
Python 3.14 verzoegert PEP 649 die Auswertung, deshalb faellt es auf der
Entwicklungsmaschine (3.14.2) nicht auf.

Beleg, gegen den echten Code gefahren:

```console
$ py -3.12 -c "import limits"
  File "limits.py", line 85, in <module>
    _429_snapshots: dict[str, tuple[ProviderLimits, float]] = {}
NameError: name 'ProviderLimits' is not defined

$ py -3.12 -c "import orchestrator"
  File "dispatcher.py", line 24, in <module>
    from limits import AllLimits, ProviderLimits, is_transient_token_refresh
  ... gleicher NameError
```

Auf 3.13 identisch. **Radius:** `limits` haengt an `dispatcher.py`,
`orchestrator.py`, `parallel_runner.py`, `telegram_listener.py` und 6 Tools —
die Anwendung startet auf keiner der Versionen, die `README.md:64` als
unterstuetzt nennt (`Python 3.10+`), ausser 3.14.

**Alter:** eingefuehrt mit `924d274` (2026-03-07, HTTP-429-Resilienz), rund ein
halbes Jahr unbemerkt. Die Testsuite kann das bauartbedingt nicht sehen: sie
laeuft nur auf dem Interpreter, der installiert ist.

**Fix** (nicht ausgefuehrt — Auftrag schliesst Produktivcode aus): Annotation
quoten (`tuple["ProviderLimits", float]`), oder `from __future__ import
annotations` setzen, oder die Zuweisung unter die Klassendefinition schieben.
Die erste Variante ist die kleinste. Danach gehoert die untere Versionsgrenze
verifiziert statt behauptet — entweder CI auf 3.10/3.12, oder `README.md`
ehrlich auf `3.14+` ziehen.

### D2 — `B023` `telegram_listener.py:942/944/945`

`_send_thinking_if_pending` schliesst ueber die Schleifenvariablen
`provider_done` und `provider`, statt sie zu binden. **Real, aber enger als es
klingt:** der `finally`-Block setzt `provider_done` und ruft
`thinking_timer.cancel()`, bevor die naechste Iteration die Namen neu bindet.
Es bleibt ein Rennen — feuert der Timer genau waehrend des `finally`, liest die
Closure das *neue*, noch nicht gesetzte Event und den *neuen* Provider-Namen.
**Folge:** eine ueberfluessige oder falsch beschriftete „denkt noch nach"-
Meldung per Telegram, kein Steuerfluss-Schaden. Fix ist ein Default-Argument
(`def _send_thinking_if_pending(_done=provider_done, _p=provider)`).

### D3 — `F402` `tools/base_tool.py:138`

`for field in (...)` ueberschattet den `dataclasses.field`-Import. Die Bindung
ist **lokal zur Methode `add()`**, und `field()` wird dort nicht aufgerufen —
**heute also kein Laufzeitfehler**, sondern eine Mine: wer spaeter in dieser
Methode `field(...)` benutzt, bekommt einen String. Umbenennen in `attr`.

### D4 — `B905` `zip()` ohne `strict=` (7×)

`queue_manager.py:1138/1147/1346`, `quota_calibration.py:502`,
`quota_calibration_backfill.py:155`, `tools/brainstorm.py:343`,
`tools/crosschecks/adversarial_search.py:188`. Nicht per se falsch, aber
`zip()` kuerzt still auf die kuerzere Seite. Gerade in `queue_manager` (Zeilen
gegen geparste Tasks) ist das genau die Klasse von stillem Datenverlust, die
dieses Repo schon mehrfach getroffen hat. Pro Stelle entscheiden:
`strict=True` oder ein Kommentar, warum die Laengen auseinanderlaufen duerfen.

### D5 — `F841` tote Werte in `dev_loop` und `review_loop`

`tools/dev_loop.py:736` (`last_quality_tuple`), `tools/review_loop.py:733`
(`last_findings_tuple`), dazu `parallel_runner.py:119` (`clean_text`) und
`tools/scientific_investigation.py:382` (`manifest_path`). Die beiden `last_*`
stehen direkt neben `seen_*_signatures.add(sig)` und werden nie gelesen — das
sieht nach einem halb verdrahteten „vergleiche mit letzter Runde"-Feature aus.
Vor dem Loeschen kurz pruefen, ob da eine Absicht begraben liegt; die anderen
beiden sind schlicht tot.

### D6 — `union-attr` auf `Optional` (11×)

`providers/process_runner.py:347/355/363/365` greifen auf `proc.stdin`
(`IO | None`) zu — strukturell durch `stdin=PIPE` ausgeschlossen, aber nur
durch Konstruktion, nicht durch eine Pruefung. `policy.py:120` (`Pattern | None`)
und `orchestrator.py:1343` (`tuple | None`) sind einzeln zu bewerten. Kein
Befund davon ist als konkreter Absturz nachgewiesen — ehrlichste Einordnung:
**ungeprueft, nicht harmlos**.

## Keine Defekte — False Positives, Annotations-Luecken, Entscheidungen

### E1 — `PLC0415` (524)

Mit Abstand die groesste Gruppe, 199 im Produktivcode (Spitzenreiter
`heartbeat.py` 24, `telegram_listener.py` 20). Das sind fast durchweg
Lazy-Imports zur Vermeidung von Zirkelimporten — ein bewusstes Muster dieses
Repos, keine Nachlaessigkeit. Kein Schwester-Repo ignoriert die Regel, deshalb
steht sie hier **selektiert** in der Baseline, statt dass ich stillschweigend
einen Ignore erfinde. **Deine Entscheidung:** global ignorieren (empfohlen,
dann faellt die Baseline von 1351 auf 827) oder als Dauer-Rauschen behalten.

### E2 — `E501` (352) und `ruff format` (156 Dateien)

`E501` ist ignoriert (eeg-Linie: „Sache des Formatters"), separat gezaehlt sind
es 352 Stellen. `ruff format --check` wuerde **156 Dateien** umschreiben.
Die Summenzeile nennt dazu 39 bereits formatierte, zusammen also 195 betrachtete
Dateien - das laesst sich nicht gegen die 176 `.py` der Scope-Tabelle oben aufloesen
und ist hier nicht weiterverfolgt; belastbar ist allein der Zaehler 156, den
`ruff format --check` auch namentlich auflistet. Das
ist ein eigener, grosser, rein mechanischer Commit — und der einzige Schritt
hier, der jeden `git blame` im Repo zerreisst. Bewusst nicht ausgefuehrt.

### E3 — `RUF100` (8)

Die Gruppe ist **gemischt**, und ruff sagt in der Meldung selbst, welcher Fall
vorliegt. Unter *dieser* Konfiguration gemessen (`ruff check .`, alle 8 Zeilen):

| n | Meldung | Stellen | Bedeutung |
|---:|---|---|---|
| 5 | `non-enabled: BLE001` | `orchestrator.py:588`, `preflight.py:334`, `tests/conftest.py:41`, `tools/base_tool.py:521/567` | dokumentierte Absicht fuer eine Regelgruppe, die ich nicht selektiert habe |
| 3 | `unused: E402` | `tests/conftest.py:16`, `tests/test_quota_calibration.py:12/13` | `E402` **ist** ueber die `E`-Gruppe aktiv — hier feuert es trotzdem nicht |

**Die 5 `BLE001`-Faelle sind False Positives.**

```python
except Exception as e:  # noqa: BLE001 — telemetry must never break the loop
```

Sie zu entfernen waere aktiv schaedlich: die Begruendung ginge verloren, und
sobald jemand `BLE` selektiert, stehen die Stellen nackt da. Optionen: `BLE`
mitselektieren (dann loesen sich die 5 Befunde von selbst auf), oder `RUF100`
ignorieren.

**Die 3 `E402`-Faelle sind echte tote Suppressions** — genau die Klasse, die es
laut erster Einschaetzung hier nicht geben sollte. Ursache ist kein Regel-Drift,
sondern ein Verhaltensunterschied zu flake8: ruffs `E402` erlaubt Imports, die
auf eine `sys.path`-Manipulation folgen. Gegen den echten Code nachgemessen:

```console
$ printf 'import sys\nx = 1\nimport os\n' > a.py
$ ruff check a.py --isolated --select E402
a.py:3:1: E402 Module level import not at top of file

$ printf 'import sys\nsys.path.insert(0, "x")\nimport os\n' > b.py
$ ruff check b.py --isolated --select E402
All checks passed!

$ sed 's/# noqa: E402.*$//' tests/test_quota_calibration.py > t.py
$ ruff check t.py --isolated --select E402
All checks passed!            # ohne noqa entsteht kein Befund
```

Von den 5 `# noqa: E402` im Repo sind also nur 2 lebendig
(`scripts/build_audit_pack.py:64`, `tests/test_test_loop.py:87` — dort steht
kein `sys.path`-Aufruf davor); die anderen 3 sind Erblast aus flake8-Gewohnheit
und koennen ersatzlos weg. `E402` selbst feuert genau einmal ungedeckt:
`config.py:844` (`import threading as _threading` mitten in der Datei).

**Nicht** anwendbar auf die Gruppe als Ganzes: `ruff check --fix --select RUF100`
haette alle 8 geloescht, also auch die 5 begruendeten.

> **Messfalle, an der die erste Fassung dieses Abschnitts gescheitert ist:**
> `ruff check . --select RUF100` liefert **12** statt 8 Befunde und eine andere
> Aufschluesselung (5× `E402`, 5× `BLE001`, 1× `F401`, 1× `B018`) — weil `--select`
> die aktivierte Regelmenge *ersetzt*, `E402` damit nicht mehr aktiv ist und die
> 5 `E402`-noqa allesamt als `non-enabled` erscheinen. `RUF100` ist die einzige
> Regel hier, deren Ergebnis von der Auswahl der *anderen* Regeln abhaengt; sie
> darf nur im vollen Konfigurationslauf gezaehlt werden.

### E4 — `SIM115` `providers/process_runner.py:608`

**False Positive.** Das Handle muss den Block ueberleben (geht als `stdin` an
`Popen`), es gibt einen `except BaseException`-Pfad, der es schliesst, und ein
`finally` weiter unten. Ein `# noqa: SIM115` mit genau dieser Begruendung ist
die richtige Antwort.

### E5 — `limits.py:1261` (`index`)

**False Positive.** `skip = cached is not None and (...)`, danach `if skip:` —
mypy kann ueber die Boolean-Variable nicht narrowen. Kein Handlungsbedarf.

### E6 — optionale Imports ohne Annotation (`heartbeat.py:1067/1082` und 3 weitere)

Der Regelname `truthy-function` legt einen toten Guard nahe. Der Quelltext sagt
das Gegenteil — die beiden mypy-Meldungen gehoeren zusammengelesen:

```python
# heartbeat.py:1064-1067
try:
    from notifier import send_message
except Exception:
    send_message = None          # :1067  [assignment]  None in Callable[[str], bool]
...
# heartbeat.py:1082
if send_message:                 # :1082  [truthy-function]
    try:
        send_message(msg)
```

Der Guard ist ein **echter `None`-Check** und verhindert
`TypeError: 'NoneType' object is not callable`, wenn der `notifier`-Import
scheitert. Ihn zu streichen waere ein eingebauter Absturz. mypy meldet ihn nur,
weil der Typ von `send_message` aus dem *gelungenen* Import abgeleitet wird —
der `except`-Zweig verletzt diesen Typ (`:1067`), und danach sieht mypy eine
Funktion, die nie falsy sein kann (`:1082`). Beide Meldungen verschwinden mit
einer Annotation:

```python
send_message: Callable[[str], bool] | None
try:
    from notifier import send_message  # type: ignore[assignment]
except Exception:
    send_message = None
```

**Dasselbe Muster an drei weiteren Stellen** (jeweils `assignment`, Modul statt
Callable): `tools/base_tool.py:528`, `tools/review_loop.py:457`,
`tools/dev_loop.py:330` — alle mit korrektem `if x is not None:`-Guard dahinter.
Das ist eine Annotations-Luecke im Repo-Muster „optionaler Import", kein Defekt:
4 von 9 `assignment`-Fehlern und der einzige `truthy-function`-Fehler kommen
daher.

### E7 — `unused-ignore` (5) — Artefakte der lenient-Konfiguration

Zuerst eine Abgrenzung, die beim Abarbeiten Geld kostet, wenn man sie uebersieht:
**`unused-ignore` ist eine mypy-Diagnose, keine ruff-Regel.** Kein
`ruff check --fix`-Kommando kann diese 5 Stellen anfassen; wer sie in ein
Autofix-Paket einsortiert, loescht sie zwangslaeufig von Hand.

Und genau das waere falsch. Die 5 Kommentare sind nicht tot, sondern **unter
dieser Konfiguration** unwirksam — sie werden wieder scharf, sobald die
Konfiguration sich in die Richtung bewegt, die Paket 12 vorschlaegt. Gemessen,
nicht aus dem Regelnamen geschlossen:

| Stelle | Kommentar | Warum aktuell unwirksam |
|---|---|---|
| `tests/test_build_audit_pack.py:121` | `# type: ignore[no-untyped-def]` | `no-untyped-def` feuert nur unter `disallow_untyped_defs` (Teil von `strict`) |
| `tests/test_build_audit_pack.py:310` | `# type: ignore[no-untyped-def]` | dito |
| `tests/test_skill_index.py:138` | `# type: ignore[arg-type]` | dito — ungeprueft, weil die Zielfunktion unannotiert ist |
| `tests/test_scientific_investigation_phases.py:318` | `# type: ignore[arg-type]` | dito |
| `tools/scientific_investigation_phases.py:210` | `import yaml  # type: ignore` | haengt an `ignore_missing_imports = true`, nicht an `strict` |

**Messung 1 — die ersten vier.** Unter `--strict` bleibt von den 5 Meldungen
genau **eine** uebrig:

```bash
python -m mypy --strict tests/test_build_audit_pack.py tests/test_skill_index.py \
  tests/test_scientific_investigation_phases.py tools/scientific_investigation_phases.py \
  --explicit-package-bases --namespace-packages | grep unused-ignore
# → nur noch tools/scientific_investigation_phases.py:210
```

Die vier Test-Kommentare werden also wieder gebraucht. Loescht man sie jetzt,
muss Paket 12 sie wieder eintragen.

**Messung 2 — der yaml-Import.** Der fuenfte haengt an einem anderen Schalter.
Mit `ignore_missing_imports` **aus** verschwindet seine `unused-ignore`-Meldung,
und an den yaml-Importstellen *ohne* Ignore-Kommentar erscheinen stattdessen
Fehler — d. h. der Kommentar in `:210` arbeitet dort tatsaechlich:

```bash
python -m mypy tools/scientific_investigation_phases.py --config-file= \
  --no-site-packages --explicit-package-bases --namespace-packages --warn-unused-ignores
# → skills/discovery.py:2: error: Library stubs not installed for "yaml"  [import-untyped]
# → policy.py:248:      error: Library stubs not installed for "yaml"  [import-untyped]
# → :210 selbst meldet nichts mehr
```

**Empfehlung: nicht anfassen.** Die 5 kosten nichts und sind korrekte
Vorsorge fuer eine strengere Konfiguration. Wer sie trotzdem bereinigen will,
tut das *zusammen mit* Paket 12 und nicht davor — dann zeigt `warn_unused_ignores`
unter der dann geltenden Konfiguration an, welche wirklich uebrig sind.

## Vorgeschlagene Arbeitsreihenfolge

Nichts davon wurde in diesem Task ausgefuehrt.

| # | Paket | Umfang | Risiko |
|---|---|---|---|
| **1** | **[D1](#d1--limitspy85--f821-undefined-name) fixen** — eine Zeile quoten, danach Versionsgrenze klaeren (CI auf 3.12 **oder** README auf 3.14+ korrigieren) | 1 Zeile + 1 Entscheidung | keins, hoher Gewinn |
| 2 | Entscheidung [E1](#e1--plc0415-524) `PLC0415` — vor allem anderen, weil sie 39 % der Baseline bewegt | 1 Konfigzeile | keins |
| 3 | `F401` + `F541` + `RUF059` autofixen (`ruff check --fix --select F401,F541,RUF059`), Diff durchsehen. **Die 5 `unused-ignore` gehoeren ausdruecklich NICHT dazu** — mypy-Diagnose, von ruff gar nicht erreichbar, und laut [E7](#e7--unused-ignore-5--artefakte-der-lenient-konfiguration) stehen zu lassen | 154 Stellen (134 davon safe-autofixbar, die 20 `RUF059` brauchen `--unsafe-fixes`) | sehr gering |
| 4 | `I001` + `UP045` + `UP037` + `UP017` autofixen — reine Modernisierung | ~230 Stellen | gering |
| 5 | [D2](#d2--b023-telegram_listenerpy942944945) `B023`, [D3](#d3--f402-toolsbase_toolpy138) `F402`, `no-redef` — Einzeiler mit Verstaendnis-Bedarf | 3 Stellen | gering, je einzeln pruefen |
| 6 | [D5](#d5--f841-tote-werte-in-dev_loop-und-review_loop) `F841` — vorher klaeren, ob `last_*_tuple` ein unfertiges Feature ist | 12 Stellen | gering |
| 7 | [D4](#d4--b905-zip-ohne-strict-7) `B905` + [D6](#d6--union-attr-auf-optional-11) `union-attr` + `PLW1510` — pro Stelle bewerten, meist bewusst | ~52 Stellen | Kopfarbeit, kein Automat |
| 8 | [E6](#e6--optionale-imports-ohne-annotation-heartbeatpy10671082-und-3-weitere) annotieren (`heartbeat.py` + 3 gleichartige Stellen) — **Guard stehen lassen**, nur den Typ nachtragen | 4 Stellen | gering; Loeschen des Guards waere ein Absturz |
| 9 | Die 3 toten `# noqa: E402` entfernen ([E3](#e3--ruf100-8)) — die 5 `BLE001`-noqa dabei **nicht** anfassen, also von Hand statt per `--fix` | 3 Stellen | keins |
| 10 | `noqa` fuer [E4](#e4--sim115-providersprocess_runnerpy608), Entscheidung `BLE` selektieren vs. `RUF100` ignorieren ([E3](#e3--ruf100-8)) | 1 Stelle + 1 Entscheidung | keins |
| 11 | Entscheidung [E2](#e2--e501-352-und-ruff-format-156-dateien) `ruff format` — als **eigener** Commit, nie mit Inhaltlichem gemischt | 156 Dateien | zerreisst `git blame` |
| 12 | Erst danach Richtung mypy `strict` (Abstand: 3851 gegen 131), sinnvoll nur modulweise ueber `[[tool.mypy.overrides]]` | gross | eigenes Projekt |

Schritte 1–2 sind die einzigen mit echtem Nutzen pro Aufwand. Alles ab 7 ist
optional und sollte nicht als Hygiene-Pflicht missverstanden werden.

## Offene Fragen

1. **`requirements-dev.txt` anlegen?** Ohne Pin sind die Zahlen dieses Berichts
   beim naechsten ruff-Release nicht reproduzierbar. Haette den Diff aber ueber
   „Konfiguration + Bericht" hinaus erweitert — deshalb hier nur gefragt.
2. **Untere Python-Grenze:** `README.md` sagt 3.10+, real laeuft nur 3.14.
   Reparieren (D1 + CI-Matrix) oder Anspruch zuruecknehmen?
3. **Pre-commit-Hook / CI-Gate** fuer ruff — sinnvoll erst, wenn die Baseline
   auf ein Niveau gebracht ist, das ein Gate ueberhaupt halten kann.
