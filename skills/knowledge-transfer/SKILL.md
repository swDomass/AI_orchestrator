---
name: knowledge-transfer
description: Cross-Domain Know-How-Transfer — findet kreative Anwendungen von Vault-Expertise in anderen Branchen
version: "1.0"
requires:
  bins: []
  env: []
  os: []
  providers: ["claude"]
tags: ["innovation", "kreativität", "cross-domain", "ideen", "vault", "wissen", "transfer"]
config:
  timeout_minutes: 60
---
## System Prompt Addition

Führe einen strukturierten Cross-Domain Know-How-Transfer in 4 Phasen durch:

1. **Vault-Scan**: Durchsuche den Vault intelligent — bevorzuge Notizen mit hoher
   Wissenstiefe (viele Wikilinks, technisches Vokabular, Projekttiefe).

2. **Know-How-Extraktion**: Identifiziere EINE tiefe Expertise-Domäne.
   Lese ZWISCHEN DEN ZEILEN — die Person hat ihr Wissen oft nicht explizit
   niedergeschrieben. Schliesse aus Projekten, Methoden und Vokabular auf
   das spezifische Handwerkszeug dahinter.

3. **Cross-Domain-Transfer**: Nutze WebSearch um konkrete Probleme in anderen
   Branchen zu finden. Sei KONKRET und SPEZIFISCH — keine generischen Aussagen.
   Die Verbindung soll überraschend und nicht-offensichtlich sein.

4. **Synthese**: Entwickle die beste Idee zu einer vollständigen Obsidian-Notiz
   und speichere sie unter 01_Ideen/<slug>/.

Optionale Themenangabe in der Queue-Zeile (Doppelpunkt-Syntax):
    - [ ] Know-How Transfer: Bremsquitschen #tool:knowledge-transfer
    - [ ] Know-How Transfer #tool:knowledge-transfer  (auto-discovery)
