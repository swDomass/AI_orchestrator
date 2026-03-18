# Plan zur Optimierung der Usage-Gating & Tool-Resilienz

Dieses Dokument beschreibt die Analyse von Schwachstellen im aktuellen Nutzungs-Management des AI Orchestrators und definiert Lösungswege für eine stabilere Ausführung von Langzeit-Tasks (Loops).

## 1. Problemanalyse

### 1.1 Cache-Staleness (Veraltete Daten)
* **Problem:** Die `cclimits` Abfrage nutzt einen 10-Minuten-Disk-Cache (`_CCLIMITS_CACHE_TTL_SEC = 600`).
* **Auswirkung:** Wenn Kapazität manuell (außerhalb des Orchestrators) verbraucht wird, sieht der Orchestrator dies erst nach bis zu 10 Minuten. Dies kann dazu führen, dass Tools gestartet werden, obwohl das Limit bereits unterschritten ist.

### 1.2 Tool-Blindheit während der Ausführung
* **Problem:** Das Gating findet nur *vor* dem Start eines Tools (z. B. `review-loop` oder `dev-loop`) statt.
* **Auswirkung:** Ein Tool mit vielen Iterationen (bis zu 20) läuft "blind" weiter, selbst wenn die Kapazität währenddessen auf 0% sinkt. Es bricht erst ab, wenn die CLI selbst hart mit einem Rate-Limit-Fehler scheitert.

### 1.3 Ungenaue Retry-Logik
* **Problem:** Der Orchestrator berechnet die Wiederaufnahme-Zeit (`#retry`) oft manuell basierend auf relativen Angaben ("45m") und einem statischen 1-Stunden-Fallback.
* **Auswirkung:** Durch den Cache-Drift (Zeit vergeht zwischen Abfrage und Task-Ende) stimmen die berechneten Zeiten oft nicht. Der Orchestrator wacht entweder zu früh (unnötiger Poll) oder viel zu spät (Idle-Zeit) auf.

---

## 2. Lösungsansätze (Konzepte)

### 2.1 Mid-Loop Validation (Throttled Check)
* **Konzept:** Tools wie der `review-loop` prüfen vor jeder neuen Iteration (Review -> Fix -> Review), ob die Kapazität noch ausreicht.
* **Optimierung:** Um 429-Fehler bei der Abfrage zu vermeiden, wird nur dann ein Check durchgeführt, wenn die letzten Daten älter als **5 Minuten** sind. Da `limits.py` einen Background-Thread hat, erfolgt dieser Check ohne zusätzliche API-Last (Lesen aus dem RAM-Cache).

### 2.2 "Pause & Resume" statt Abbruch
* **Konzept:** Anstatt bei Token-Mangel mit `success=False` abzubrechen, geht das Tool in einen `SUSPENDED`-Status über.
* **Voraussetzung:** Der Fortschritt muss persistent gespeichert werden (z. B. in einer `.ai_state.json` im Projektordner oder durch Checkpointing im Task-Text der Obsidian-Queue).
* **Vorteil:** Beim nächsten Start des Orchestrators (nach dem Reset) macht das Tool genau dort weiter, wo es aufgehört hat, anstatt teure Research-Phasen zu wiederholen.

### 2.3 Präzise Reset-Abfrage (Direct Mapping)
* **Konzept:** Der Orchestrator nutzt die exakten Reset-Zeiten aus dem JSON-Output von `cclimits` für den `#retry`-Tag.
* **Vorteil:** Eliminierung des Cache-Drifts. Wenn `cclimits` sagt "Reset um 15:45", wird der Task exakt auf diesen Zeitpunkt terminiert.

---

## 3. Strategische Empfehlungen (Zukünftige Umsetzung)

1. **Implementierung von `Provider.is_available()` Checks** innerhalb der `run()` Methoden der Tools (Review, Dev, Research).
2. **Einführung einer State-Management-Klasse**, die Zwischenergebnisse von Phasen-Tools (Dev-Loop) im Workspace sichert.
3. **Optimierung von `orchestrator.py`**, um bei Kapazitätsmangel nicht das gesamte System zu stoppen, sondern gezielt nur betroffene Provider zu pausieren und alternative Provider (z. B. Gemini als Fallback für Claude) aggressiver zu prüfen.

---
*Erstellt am: 2026-03-18*
*Status: Geplant / Analyse abgeschlossen*
