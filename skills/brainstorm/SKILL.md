---
name: brainstorm
description: Multi-persona Round-Table mit domain-aware Personas (LLM wählt 4–6 themenspezifische Rollen); iterative Cross-Pollination bis TF-IDF-Konvergenz; Synthesizer ranked Top-N mit Pro/Contra
version: "1.0"
requires:
  bins: []
  env: []
  os: []
  providers: []
tags: ["brainstorm", "ideation", "round-table", "multi-persona", "cross-pollination", "ranking"]
config:
  timeout_minutes: 180
---
## System Prompt Addition

Du führst eine Brainstorming-Round-Table-Session mit dynamisch generierten Personas durch.

**Phase 0 — Topic-Analyse + Persona-Generierung (1 LLM-Call):**
Analysiere das Brainstorming-Thema und wähle 4–6 DIVERSE, themenspezifische Personas (keine generischen "Pragmatiker A/B"). Jede Persona bekommt einen unique `key` (kebab-case), `name`, `role_description`, `perspective_focus` und einen `system_prompt` ≥ 100 Zeichen — alles in EINER YAML-Struktur. Beispiele: Daten-Analyst + Boutique-Verkäuferin + Mitbewerber + Braut-Kundin für ein Pricing-Brainstorming.

**Phase 0.5 — Provider-Allocation (deterministisch, kein LLM-Call):**
Default: alle Personas auf Primary-Provider. Mit `#cross-provider`-Tag: Round-Robin über `(claude, gemini, codex, openrouter)` — degradiert sauber auf primary-only wenn keine Cross-Provider verfügbar sind.

**Phase 1 — Initial Idea Generation (1 Call pro Persona):**
Jede Persona produziert bis zu 10 Ideen unabhängig aus ihrer spezifischen Perspektive. Output strukturiert in ```` ```ideas ```` -Block mit Nummerierung. Quantität vor Qualität.

**Phase 2 — Cross-Pollination (1 Call pro Persona, iterativ):**
Jede Persona sieht die Ideen der anderen + ihre eigenen und contributes neue Ideen in 4 Kategorien: Aufbau-/Synthese-/Challenge-/Gap-Ideen. Keine Wiederholung eigener vorheriger Ideen.

**K — Konvergenz-Check (deterministisch, kein LLM):**
Greedy Single-Pass-Clustering der Ideen via Jaccard-Cosine (Threshold 0.40). Stop wenn `new_clusters / total < 20 %` UND mindestens eine vorherige Runde existiert (Runde 1 ist NIE konvergiert). Hard-Cap: 5 Iterationen.

**Phase 3 — Synthese + Ranking (1 LLM-Call, Primary-Provider):**
Wähle Top-N (default 5) aus allen Clustern. Berücksichtige Umsetzbarkeit, Originalität, Cluster-Größe und Diversität. Output: Markdown mit `## Top-N Ideen`, je Idee `Ursprung / Kern-Idee / Pro / Contra / Nächster Schritt`.

**Output:**
- Final-Report: `docs/brainstorm-YYYYMMDD-HHMMSS.md`
- State + Per-Iteration-Files: `.brainstorm/{ts}/`
- JSONL-Trace: `.brainstorm/traces/<run_id>.jsonl`

**Tags:**
- `#cross-provider` — Persona-Allocator über alle Provider verteilen (opt-in)
- `#max_iterations:N` (1–10, default 5)
- `#top_n:N` (1–20, default 5)
- `#min_personas:N` / `#max_personas:N` (2–10, default 4 / 6)

**Hard Rules:**
- Alle Persona/Synthese-Calls sind read-only — kein Tool darf Dateien im CWD modifizieren ausser dem finalen Report.
- Persona-Failures (eine Persona crashed) sind non-fatal — Tool macht mit Rest weiter, markiert Failure im Trace.
- Empty-Topic (alle Tags entfernt → leerer String) wird VOR dem ersten LLM-Call abgefangen.
