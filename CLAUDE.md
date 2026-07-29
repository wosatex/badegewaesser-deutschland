# Projektkontext

Statische Web-App, die die Wasserqualität amtlich überwachter Badestellen in allen
16 Bundesländern zeigt. GitHub Pages, eine GitHub Action holt die Daten täglich.
Nachfolger einer funktionierenden Berlin/Brandenburg-Version.

## Wichtigste Randbedingung

Das ist eine App über Gesundheitsdaten. **Nie Messwerte erfinden, schätzen,
interpolieren oder aus Vorjahren fortschreiben.** Fehlt ein Wert, bleibt er `None`
und die Stelle fällt auf eine niedrigere Vertrauensstufe. Eine leere Anzeige ist
richtig, eine plausibel aussehende falsche Zahl ist ein Schaden.

## Stand

```
index.html                  App, eine Datei. Bewertungslogik in bewerte(). Getestet.
scripts/build_data.py       Aggregator, ein Konnektor je Land. NICHT getestet.
sw.js, manifest.webmanifest Offline-Hülle
.github/workflows/update-data.yml   täglich 04:17 UTC
data/badestellen.json       schreibt die Action, nie von Hand pflegen
```

Das Frontend ist fertig und geprüft: Bundesland-Dropdown, Suche, Liste, Karte,
Offline-Cache, elf bestandene Tests der Bewertungslogik. **Daran ist nichts zu tun,
solange sich der Datenvertrag nicht ändert.**

Der Aggregator ist das offene Ende. Er wurde ohne Netzzugang zu den Länderportalen
geschrieben. Die Endpunkt-URLs sind recherchiert und stimmen sehr wahrscheinlich,
**die Feldnamen sind geraten**.

## Auftrag

Konnektoren gegen die echten Endpunkte testen und zum Laufen bringen, ein Land nach
dem anderen. Reihenfolge nach Aufwand-Nutzen:

1. **EEA** (`konnektor_eea`) — die Basis für alle 16 Länder. Hat Vorrang: läuft sie
   nicht, ist die App leer. Layer-ID und Feldnamen mit `?f=json` am Dienst prüfen.
2. **SH, HH, MV, ST** — Endpunkte recherchiert, nur Feldnamen korrigieren.
3. **BE, BB** — `raise NotImplementedError`. Ich habe funktionierenden Code aus der
   Vorgänger-App; frag danach, bevor du etwas Neues baust.
4. **BW, NW, NI, RP, HE, SN, TH, HB** — Portal bekannt, Endpunkt muss ermittelt werden.
5. **SL, BY** — bewusst nicht automatisiert, siehe Docstrings. Nicht anfassen.

## Arbeitsweise

- Ein Land pro Durchgang. `python scripts/build_data.py --nur SH --status`
- Vor dem Anpassen erst **die echte Antwort ansehen**: rohes JSON/GML/CSV in eine
  Datei schreiben, Feldnamen lesen, dann den Konnektor daran anpassen. Nicht raten,
  nicht mit try/except über unbekannte Strukturen bügeln.
- `DEBUG=1` setzt volle Tracebacks.
- Ein Commit je Land, Nachricht `Konnektor XX: <was jetzt geht>`.
- Wenn ein Land nicht in vertretbarem Aufwand geht: Docstring mit dem konkreten
  Hindernis aktualisieren, `NotImplementedError` stehen lassen, weiter zum nächsten.
  Ein ehrliches TODO ist besser als ein Konnektor, der stillschweigend Müll liefert.

## Datenvertrag — nicht ändern ohne Rücksprache

Jeder Konnektor gibt eine Liste aus `stelle()` zurück. Das Feld `vertrauen` steuert
die gesamte Darstellung und ist die wichtigste Entscheidung je Land:

| Wert | wann | Anzeige |
|---|---|---|
| `berechnet` | Einzelmesswerte (mindestens E. coli **und** Enterokokken) liegen vor | Punktzahl 0–100 |
| `amtlich` | nur Beurteilung/Ampel, keine Keimzahlen | Einstufung unverändert + Link |
| `jahresnote` | nichts Aktuelles maschinenlesbar | EU-Note aus dem Berichtsjahr |

Im Zweifel die **niedrigere** Stufe wählen.

`ampel` ist `gruen|gelb|rot|unbekannt` und deckelt die Punktzahl (gelb ≤58, rot ≤25).
Eine EU-Jahreseinstufung ist **keine** Tagesampel — „ausgezeichnet" auf `gruen` zu
mappen wäre falsch, dafür gibt es `einstufung_ampel()`.

Länderkonnektoren **reichern die EEA-Basis an, sie ersetzen sie nicht**. `verschmelze()`
setzt nur befüllte Werte. Wer das umgeht, verliert die Stammdaten der Länder ohne
eigenen Konnektor.

## Bekannte Fallen

- **SH** — Portal hinter Anubis (Proof-of-Work-Bot-Schutz). HTML-Seiten liefern
  „Access Denied". Ob `/collection/*.csv` ausgenommen ist, muss der erste Lauf zeigen.
  CSV ist **ISO-8859-1 mit Pipe als Trenner**, nicht UTF-8/Komma. Der Name der
  Messwert-Collection ist unbestätigt, zwei Kandidaten stehen im Code.
- **BW** — läuft auf ArcGIS Experience Builder (`?data_id=dataSource_17-…`). Netzwerk-Tab
  öffnen, XHR mitlesen, dahinter liegt ein FeatureServer mit `/query?f=geojson`. Dann
  `konnektor_st()` als Vorlage kopieren.
- **ST** — der dokumentierte WMS liegt unter `/arcgis/services/…/WMSServer`; derselbe
  Dienst ist bei ArcGIS meist auch unter `/arcgis/rest/services/…` erreichbar. Layer-Index
  kann von `0` abweichen.
- **NRW** — löst an Flussbadestellen bei über 5 mm Tagesniederschlag **automatisch ein
  Badeverbot** aus. Das ist der einzige tagesaktuelle Wert im Land und muss als
  `hinweise=[{"art":"warnung",…}]` durchgereicht werden, sonst zeigt die App an
  gesperrten Stellen grün.
- **NI** — bestes Scraping-Ziel, URL-Schema ist deterministisch:
  `batlas/index.php?p=sa` für die ID-Liste, `?p=bm&b=<EU-ID>` für Messdaten. Kein
  Session-Handling nötig. **Vorher den Menüpunkt „Downloads" im batlas prüfen** — evtl.
  gibt es die Daten fertig als Datei.
- **HE** — erst das HLNUG-Dienstverzeichnis prüfen (`hlnug.de/themen/wasser/daten-und-viewer`).
  Liegt dort ein Badegewässer-WFS, spart das den ganzen Scraper.
- **MV** — WFS liefert vermutlich nur Geometrie und Einstufung. Keimzahlen der laufenden
  Saison stehen auf `badewasser-mv.de`. Erst prüfen, was der WFS wirklich hat.
- **Küstenländer** — `typ="kueste"` setzen (SH, MV, NI, HH, HB an Nord-/Ostsee). Die
  EU-Grenzwerte sind dort strenger; falscher Typ macht die Bewertung zu milde.

## Grenzen

- **Keine Bilder und keine Profiltexte übernehmen.** Der niedersächsische
  Badegewässer-Atlas stellt sie unter „Alle Rechte vorbehalten". Messwerte sind Fakten
  und unproblematisch, Fotos nicht.
- **Höflich abrufen.** Einmal täglich reicht — die Behörden messen alle zwei bis vier
  Wochen. Bei Scrapern Pause zwischen Requests, `robots.txt` respektieren. Kein
  Parallel-Hammering von Behördenservern.
- **Den Demo-Fallback nicht entfernen.** Ohne `data/badestellen.json` muss die App
  weiterhin die als solche markierten Demo-Daten zeigen, nicht eine leere Seite.
- **Die Reissleine im Workflow nicht lockern.** Bricht der Lauf unter 1.500 Stellen ab,
  ist das ein kaputter Konnektor, keine geschrumpfte Realität. Lieber alte Daten
  behalten als eine halbleere Datei veröffentlichen.
- User-Agent in `build_data.py` oben trägt Platzhalter (`DEIN-NAME`, `DEINE-MAIL`) —
  vor dem ersten scharfen Lauf ausfüllen.

## Fertig-Kriterium

`python scripts/build_data.py` läuft durch, schreibt ≥1.500 Stellen, und in der
Konsolenausgabe steht für jedes Land entweder eine Zahl oder ein bewusstes
„Konnektor fehlt". Danach `python3 -m http.server 8000` und prüfen, dass das Dropdown
alle Länder mit Daten zeigt und die Karte Marker setzt.
