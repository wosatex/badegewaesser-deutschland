# Badewasser Deutschland

Wasserqualität der amtlich überwachten Badestellen in allen 16 Bundesländern.
Statische Seite für GitHub Pages, die Daten holt eine GitHub Action einmal täglich.

Nachfolger der Berlin/Brandenburg-Version — gleiche Grundidee, gleiche
Bewertungslogik, nur über 16 statt 2 Datenlagen.

---

## Aufbau

```
index.html                     die App, eine Datei, keine Build-Kette
sw.js                          Offline: Hülle gecacht, Daten network-first
manifest.webmanifest
data/badestellen.json          von der Action geschrieben, nicht von Hand pflegen
data/manuell/sl.json           optional: Saarland (3 Gewässer, nur PDFs)
scripts/build_data.py          Aggregator, ein Konnektor je Land
.github/workflows/update-data.yml
```

Die App lädt genau eine Datei und behält sie im Offline-Speicher. Am See
funktioniert sie ohne Empfang; nur die Kartenkacheln brauchen Netz.

---

## Loslegen

```bash
git clone https://github.com/DEIN-NAME/badestellen-de
cd badestellen-de

# Ansehen (file:// scheitert an CORS, deshalb ein Server)
python3 -m http.server 8000
# -> http://localhost:8000

# Daten holen
pip install requests
python scripts/build_data.py --out data/badestellen.json

# einzelnes Land testen
python scripts/build_data.py --nur SH --status
```

Ohne `data/badestellen.json` zeigt die App **Demo-Daten** mit erfundenen Orten
und erfundenen Werten, deutlich als solche markiert. Das ist Absicht: so lässt
sich die Oberfläche prüfen, ohne dass jemand erfundene Keimzahlen für echt hält.

### Auf GitHub Pages veröffentlichen

1. **Settings → Pages → Source: Deploy from a branch**, Branch `main`, Ordner `/`
2. **Settings → Actions → General → Workflow permissions: Read and write**
   (sonst kann die Action die Datendatei nicht committen)
3. **Actions → „Badegewässerdaten aktualisieren" → Run workflow** für den ersten Lauf

Danach läuft der Job täglich um 04:17 UTC.

---

## Einen Konnektor bauen

Jeder Konnektor ist eine Funktion, die eine Liste normalisierter Objekte
zurückgibt. Ein Fehler in einem Land bricht den Gesamtlauf nie ab — bei 16
Quellen fällt statistisch ständig eine aus.

```python
def konnektor_xx() -> list[dict]:
    r = hole("https://…/query", params={"where": "1=1", "f": "geojson"})
    return [
        stelle("XX", p["id"], p["name"],
               gewaesser=p.get("see"), lat=…, lon=…,
               vertrauen="berechnet",       # berechnet | amtlich | jahresnote
               ampel="gruen",               # gruen | gelb | rot | unbekannt
               probe="2026-07-24",
               ecoli=12, entero=4, sichttiefe=1.8)
        for p in r.json()["features"]
    ]
```

`vertrauen` steuert die Darstellung und ist die wichtigste Entscheidung:

| Wert | Bedeutung | Anzeige |
|---|---|---|
| `berechnet` | Einzelmesswerte liegen vor | Punktzahl 0–100 mit Pegelbalken |
| `amtlich` | nur Beurteilung, keine Keimzahlen | Einstufung unverändert + Link |
| `jahresnote` | nichts Aktuelles maschinenlesbar | EU-Note aus dem Berichtsjahr |

Der EEA-Layer läuft immer zuerst und legt für alle ~2.290 Stationen Stammdaten,
Jahresnote und den Link auf die Landesseite an. Länderkonnektoren **reichern an,
sie ersetzen nicht** — was ein Land nicht liefert, bleibt auf dem EEA-Stand.

---

## Stand der Konnektoren

| Land | Quelle | Stand |
|---|---|---|
| SH | `opendata.schleswig-holstein.de` CSV | recherchiert, ungetestet |
| HH | `gateway.hamburg.de` WFS | recherchiert, ungetestet |
| MV | `geodaten-mv.de` WFS | recherchiert, ungetestet |
| ST | `geodatenportal.sachsen-anhalt.de` ArcGIS REST | recherchiert, ungetestet |
| BE | Lageso-CSV | **bestehenden Code einhängen** |
| BB | LAVG-KML | **bestehenden Code einhängen** |
| BW, NW, NI, RP, HE, SN, TH, HB | Portal bekannt, Endpunkt offen | TODO |
| SL | nur PDFs, 3 Gewässer | von Hand oder EEA-Note |
| BY | 96 Kreisbehörden, keine Zentralstelle | bleibt bei EEA-Note |

Die Konnektoren mit Stand „recherchiert, ungetestet" enthalten die echten
Endpunkte, aber die Feldnamen sind geraten. Erster Lauf mit `DEBUG=1` und
`--nur SH` zeigt, was wirklich zurückkommt. Details und die offenen Punkte
stehen im Recherchedokument.

**Zwei Fallstricke, die schon bekannt sind:**
- **SH** steht hinter Anubis (Proof-of-Work-Bot-Schutz). Ob die CSV-Endpunkte
  ausgenommen sind, zeigt der erste Lauf.
- **BW** läuft auf ArcGIS Experience Builder. Netzwerk-Tab öffnen, die
  XHR-Requests mitlesen — darunter liegt ein FeatureServer.

---

## Wie die Zahl entsteht

Portiert aus der Berlin-Version, erweitert um getrennte Grenzwerte für
Binnen- und Küstengewässer. Start bei 100, abgezogen wird für E. coli und
Enterokokken (bis 48), Chlorophyll a nach den UBA-Leitwerten 40 und 100 µg/l
(bis 42), Sichttiefe (bis 18) und amtliche Hinweise (18 bzw. 5).

Die amtliche Ampel deckelt: gelb höchstens 58, rot höchstens 25. Die Bewertung
fällt nie besser aus als die des Amts. Die Logik steht in `index.html` unter
`bewerte()` und lässt sich dort in einem Stück lesen und ändern.

Ein Punkt, der sich mit 16 Ländern verschärft: Die EU-Einstufung beruht nur auf
zwei Leitindikatoren. Ein See kann „ausgezeichnet" eingestuft und gleichzeitig
wegen Cyanobakterien gesperrt sein. Genau dafür ist die Ampel-Deckelung da.

---

## Recht

Messwerte sind Fakten und nicht schutzfähig. Das Risiko liegt beim Drumherum.

- **Lizenzen** unterscheiden sich je Land: MV „keine Bedingungen", HH und SN
  DL-DE-BY 2.0 (Namensnennung), BfG GeoNutzV. Quellenvermerke gehören sichtbar
  in die App.
- **Keine Bilder und keine Profiltexte übernehmen.** Der niedersächsische
  Badegewässer-Atlas stellt sie ausdrücklich unter „Alle Rechte vorbehalten".
- **Höflich scrapen.** Einmal täglich reicht, die Behörden messen alle zwei bis
  vier Wochen. Sprechender User-Agent mit Kontaktadresse — in `build_data.py`
  ganz oben eintragen. Das ist nicht nur Anstand, es ist das Argument, wenn man
  später offiziellen Datenzugang anfragt.
- **Für Anfragen an Behörden:** EU-Durchführungsverordnung 2023/138 zu
  hochwertigen Datensätzen, Kategorie Umwelt. Verpflichtet zur
  maschinenlesbaren Bereitstellung bereits erhobener Daten, möglichst per API.

---

## Haftung

Messwerte sind eine Momentaufnahme. Nach Starkregen oder in Hitzeperioden kann
sich die Lage innerhalb von Stunden ändern. Diese Skala ist eine Lesehilfe,
keine Behördenauskunft — vor Ort gilt das Hinweisschild und die Auskunft des
Gesundheitsamts.
