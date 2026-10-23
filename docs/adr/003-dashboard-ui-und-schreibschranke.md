# ADR-003: Dashboard-Frontend aus `dashboard_ui/` mit fester Liste, Schreibschranke und Vendor-Pin

- **Status:** accepted
- **Datum:** 2026-10-23
- **Betrifft:** AI_orchestrator / `dashboard.py`, `dashboard_ui/`, `tests/test_dashboard_*`

## Kontext

Das Dashboard soll künftig den Orchestrator vollständig bedienen: Queue bearbeiten, Freigaben
erteilen, Einstellungen setzen, Onboarding. Das gilt auch für weitere Nutzer, die jeweils eine eigene
lokale Installation haben. Bis dahin war es eine reine Anzeige:
- nur `do_GET`
- einfädiger Server
- die ganze Seite als ein Inline-String `_HTML_PAGE` (~1100 Zeilen) mit Inline-`onclick`
- Chart.js von jsdelivr, ohne SRI
- kein CSP, kein Token

Ein schreibender lokaler Server ist für jede Webseite im selben Browser erreichbar, etwa über CSRF
oder DNS-Rebinding. Gebraucht wird eine Schranke, bevor der erste fachliche Schreib-Endpunkt
entsteht.

## Entscheidung

**Frontend und Auslieferung**
- Die Seite liegt in `dashboard_ui/` als klassische Skripte: `core.js` → `overview.js` →
  `harness.js` → `app.js`. Ein delegierter Klick-Listener verteilt über `data-action` und
  `registerActions`.
  *Nachtrag 2026-10-23 (B1): zusätzlich `queue.js` vor `app.js`, siehe ADR-004.*
- Ausgeliefert wird nur über die feste Tabelle `_STATIC_FILES` mit exaktem Pfadabgleich.
- Chart.js 4.6.1 und chartjs-plugin-zoom 1.2.1 liegen gepinnt (SHA-256 im Test) unter
  `dashboard_ui/vendor/`.
- Jede Antwort trägt eine strikte CSP ohne `unsafe-*`, dazu `frame-ancestors 'none'`, DENY,
  nosniff, no-referrer und no-store.

**Server**
- Der Server ist mehrfädig: `ThreadingMixIn` plus `TCPServer`. `_DATA_BUILD_LOCK` sorgt dafür, dass
  höchstens eine Analytics-Berechnung gleichzeitig läuft.

**Schreibschranke**

Jeder POST durchläuft `do_POST` mit genau dieser Reihenfolge. Die Prüfungen 1 bis 4 laufen auf den
Headern, bevor ein Body-Byte gelesen wird.
1. Host ist Loopback mit dem gebundenen Port.
2. Origin gehört zur Loopback-Menge **und** ist gleich `http://` + Host.
3. `Sec-Fetch-Site` ist `same-origin`, falls vorhanden.
4. `X-Orch-Token` stimmt. Der Schlüssel wird je Start erzeugt und nur bei Loopback-Host in die Seite
   eingebettet.
5. Die Route existiert.
6. Content-Type ist JSON.
7. Content-Length ist vorhanden und gültig.
8. Der Body ist höchstens 256 KiB groß.
9. Der Body ist vollständig.
10. Er ist ein JSON-Objekt.
11. Die Route läuft.

Antworten haben immer die Form `{"ok", "error": <fester Text>, "code"}` und enthalten nie `str(e)`.
Spätere Bausteine registrieren Endpunkte als reine Funktionen per `@post_route("/api/…")`. Fehler
melden sie über `ApiError`, dessen Konstruktor Status und Texte prüft.

## Alternativen

- **(a) Inline lassen, Nonce-CSP:** verworfen. Inline-`onclick` braucht `'unsafe-hashes'`, und
  1100 Zeilen JS in einem Python-String sind weder lint- noch direkt testbar. Chart.js käme
  trotzdem von außen.
- **(b) Generischer Datei-Server** (`SimpleHTTPRequestHandler`, Traversal-Prüfung): verworfen. Ein
  einziger Normalisierungsfehler unter Windows (`..\`, ADS, Groß/Klein, 8.3-Namen) öffnet das
  Repo samt `.env`. Die feste Tabelle schließt Traversal schon durch ihren Aufbau aus.
- **(c) Cookie- oder Session-Token:** verworfen. Der Browser schickt Cookies automatisch mit, das
  ist das klassische CSRF-Problem. Außerdem trennen Cookies nicht nach Port.
- **(d) `http.server.ThreadingHTTPServer`:** verworfen. Er setzt `allow_reuse_address = True`, und
  `SO_REUSEADDR` ließe unter Windows einen zweiten Dashboard-Prozess denselben Port binden.
- **(e) ES-Module statt klassischer Skripte:** verworfen. Sie brechen das Prüfmodell der
  node-Tests, die die Skripte aneinanderhängen und globale Funktionen aufrufen.

## Konsequenzen

- **+** Das Dashboard geht offline und lädt keinen fremden Code nach.
- **+** Die CSP ist strikt.
- **+** Ein fremder Browser-Tab kommt weder durch Formular-POST, no-cors-fetch, Rebinding noch per
  iframe an der Schranke vorbei.
- **+** Neue Schreib-Endpunkte brauchen eine Zeile Registrierung.
- **−** Jede neue UI-Datei braucht einen Eintrag in `_STATIC_FILES` und den Test dazu.
- **−** Vendor-Updates sind Handarbeit. Der Ablauf steht in `dashboard_ui/vendor/README.md`.
- **−** Offene Tabs müssen nach einem Orchestrator-Neustart neu geladen werden: Ein alter Schlüssel
  bekommt 403 `token`.
- **−** Der Schlüssel schützt nicht gegen lokale Prozesse, denn die können `GET /` lesen. Netzzugriff
  und Login sind bewusst kein Ziel.
- **−** Verschieben per Ziehen (hammerjs) bleibt aus, wie bisher. Ein Mitliefern wäre eine sichtbare
  Verhaltensänderung und eine eigene Entscheidung.
