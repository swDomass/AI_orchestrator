# Vendorte Bibliotheken des Dashboards

Die Dateien liegen unverändert (Bytes wie im npm-Paket) im Repo, damit das Dashboard
ohne CDN auskommt. Nicht von Hand ändern; `.gitattributes` schaltet die Zeilenenden-
Umwandlung für diesen Ordner ab. Abrufdatum aller Dateien: 2026-10-23.

## chart.js 4.6.1
- Datei: `chart.js-4.6.1/chart.umd.min.js` (aus `package/dist/`), SHA-256
  `87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2` (210 082 B)
- Lizenz: MIT, `chart.js-4.6.1/LICENSE.md` (SHA-256
  `41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce`)
- Quelle: https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz
- Gegenprobe: https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js (gleicher Hash)

## chartjs-plugin-zoom 1.2.1
- Datei: `chartjs-plugin-zoom-1.2.1/chartjs-plugin-zoom.min.js` (aus `package/dist/`),
  SHA-256 `711b558fad87393d4cdc419cfb810169c53fdfc858b18acd041e3ccc4567f2e5` (11 987 B)
- Lizenz: MIT, `chartjs-plugin-zoom-1.2.1/LICENSE.md` (SHA-256
  `faf50ba4a21e0c740c96e9bbce1b862273cfe5dd2cdf200d3271a781094c6ba9`)
- Quelle: https://registry.npmjs.org/chartjs-plugin-zoom/-/chartjs-plugin-zoom-1.2.1.tgz
- Gegenprobe: https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@1.2.1/dist/chartjs-plugin-zoom.min.js

Die Lizenzdateien hießen im Tarball bereits `LICENSE.md`.

## Warum kein hammerjs
Die bisherige CDN-URL `hammerjs@2/dist/hammer.min.js` lieferte 404. Das Zoom-Plugin
startet Hammer nur, wenn es vorhanden ist; Verschieben per Ziehen war also nie aktiv.
Würden wir hammerjs jetzt mitliefern, änderte sich das Verhalten sichtbar. Die Version
2.0.8 läge außerdem im Paket-Wurzelverzeichnis, nicht unter `dist/`.

## Update-Ablauf
1. `npm pack <paket>@<neue-version>` in einem leeren Ordner, entpacken, nichts ausführen.
2. Neuen Ordner mit der Versionsnummer anlegen, Dateien und LICENSE.md unverändert kopieren.
3. Hash gegen jsdelivr prüfen; die feste Liste `_STATIC_FILES` in `dashboard.py` und den
   Hash-Test anpassen. Ebenso die beiden `<script src>` in `dashboard_ui/index.html` und
   `_VENDOR` in `tests/_ui_scripts.py` auf die neuen Ordnernamen umstellen.
4. Alten Versionsordner entfernen, diese README aktualisieren.
