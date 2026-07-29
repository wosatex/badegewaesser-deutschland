#!/usr/bin/env python3
"""
build_data.py — führt die Badegewässerdaten der 16 Bundesländer zu einer Datei zusammen.

Aufruf:
    python scripts/build_data.py --out data/badestellen.json
    python scripts/build_data.py --out data/badestellen.json --nur SH,HH,MV
    python scripts/build_data.py --out data/badestellen.json --status   # nur Verfügbarkeit prüfen

Architektur
-----------
Jeder Konnektor ist eine Funktion, die eine Liste von Stelle-Objekten zurückgibt
oder eine Exception wirft. Ein Fehler in einem Land darf den Gesamtlauf nie
abbrechen — bei 16 Quellen fällt statistisch ständig eine aus. Fällt ein Land
aus, wird der letzte erfolgreiche Stand aus der bestehenden Ausgabedatei
übernommen und im Feld `quellen` als veraltet markiert.

Der EEA-Layer läuft immer zuerst und liefert für alle ~2.290 Stationen
Stammdaten, EU-Jahreseinstufung und den Link auf die Landesseite. Länder-
konnektoren reichern diese Basis an; sie ersetzen sie nicht.

Verifikationsstand
------------------
[OK]     Endpunkt recherchiert und Formatstruktur bekannt
[PRUEF]  Endpunkt recherchiert, Antwortformat noch nicht gegen echte Daten getestet
[TODO]   Portal bekannt, Endpunkt muss noch aus dem Netzwerk-Tab gezogen werden
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable

import requests

UA = "badestellen-de/1.0 (+https://github.com/DEIN-NAME/badestellen-de; Kontakt: DEINE-MAIL)"
TIMEOUT = 45

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

LAENDER = ["BW", "BY", "BE", "BB", "HB", "HH", "HE", "MV",
           "NI", "NW", "RP", "SL", "SN", "ST", "SH", "TH"]

# EU-Präfix je Land, für die Zuordnung der EEA-Datensätze
EU_PREFIX = {
    "DEBW": "BW", "DEBY": "BY", "DEBE": "BE", "DEBB": "BB", "DEHB": "HB",
    "DEHH": "HH", "DEHE": "HE", "DEMV": "MV", "DENI": "NI", "DENW": "NW",
    "DERP": "RP", "DESL": "SL", "DESN": "SN", "DEST": "ST", "DESH": "SH",
    "DETH": "TH",
}


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def hole(url: str, **kw) -> requests.Response:
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def stelle(land: str, sid: str, name: str, **kw) -> dict:
    """Normalisiertes Stelle-Objekt. Alle Konnektoren geben genau diese Struktur zurück."""
    return {
        "id": sid,
        "land": land,
        "name": name,
        "gewaesser": kw.get("gewaesser"),
        "ort": kw.get("ort"),
        "typ": kw.get("typ", "binnen"),            # "binnen" | "kueste"
        "lat": kw.get("lat"),
        "lon": kw.get("lon"),
        "vertrauen": kw.get("vertrauen", "jahresnote"),  # berechnet | amtlich | jahresnote
        "einstufung": kw.get("einstufung"),
        "ampel": kw.get("ampel", "unbekannt"),     # gruen | gelb | rot | unbekannt
        "probe": kw.get("probe"),                  # ISO-Datum der letzten Probe
        "messwerte": {
            "ecoli": kw.get("ecoli"),
            "entero": kw.get("entero"),
            "chlorophyll": kw.get("chlorophyll"),
            "sichttiefe": kw.get("sichttiefe"),
            "temperatur": kw.get("temperatur"),
        },
        "hinweise": kw.get("hinweise") or [],
        "url": kw.get("url"),
    }


def einstufung_ampel(e: str | None) -> str:
    """EU-Jahreseinstufung ist keine Tagesampel. Konservativ abbilden."""
    if not e:
        return "unbekannt"
    e = e.lower()
    if "mangel" in e or "poor" in e:
        return "rot"
    if "ausreichend" in e or "sufficient" in e:
        return "gelb"
    return "unbekannt"   # ausgezeichnet/gut sagen nichts über heute aus


# ==========================================================================
# BASIS — EEA Discomap, EU-weit
# ==========================================================================

EEA_URL = ("https://marine.discomap.eea.europa.eu/arcgis/rest/services/"
           "BathingWater/BathingWater_Dyna_WM_2018/MapServer")


def konnektor_eea() -> list[dict]:
    """
    [PRUEF] ArcGIS-REST. Liefert Name, Koordinaten, Link auf das nationale
    Badegewässerprofil und die Bewertungsstatus.

    Vor dem ersten Lauf einmal von Hand aufrufen:
        {EEA_URL}?f=json                 -> welche Layer-ID trägt "bathing water"?
        {EEA_URL}/<id>?f=json            -> welche Feldnamen genau?
    Die Feldnamen unten sind der übliche WISE-Satz, können aber je nach
    Dienstversion abweichen. LAYER_ID notfalls anpassen.
    """
    LAYER_ID = 1
    params = {
        "where": "bwid LIKE 'DE%'",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": 2000,
    }

    stellen, offset = [], 0
    while True:
        params["resultOffset"] = offset
        r = hole(f"{EEA_URL}/{LAYER_ID}/query", params=params)
        gj = r.json()
        feats = gj.get("features", [])
        if not feats:
            break

        for f in feats:
            p = f.get("properties") or {}
            geom = (f.get("geometry") or {}).get("coordinates") or [None, None]

            bwid = p.get("bwid") or p.get("BWID") or p.get("bathingWaterIdentifier")
            if not bwid:
                continue
            land = EU_PREFIX.get(str(bwid)[:4].upper())
            if not land:
                continue

            art = (p.get("bwWaterType") or p.get("waterType") or "").lower()
            typ = "kueste" if ("coast" in art or "transitional" in art) else "binnen"
            eins = p.get("quality") or p.get("bwQuality") or p.get("classification")

            stellen.append(stelle(
                land, str(bwid), p.get("bwName") or p.get("name") or str(bwid),
                gewaesser=p.get("rbdName") or p.get("waterBodyName"),
                ort=p.get("municipality") or p.get("nutsName"),
                typ=typ,
                lat=geom[1], lon=geom[0],
                vertrauen="jahresnote",
                einstufung=eins,
                ampel=einstufung_ampel(eins),
                url=p.get("bwProfileUrl") or p.get("profileUrl") or p.get("url"),
            ))

        if len(feats) < params["resultRecordCount"]:
            break
        offset += len(feats)
        time.sleep(0.4)

    return stellen


# ==========================================================================
# KATEGORIE A — echte Schnittstellen mit Messwerten
# ==========================================================================

SH_BASIS = "https://opendata.schleswig-holstein.de/collection"


def _sh_csv(name: str) -> list[dict]:
    """SH liefert ISO-8859-1 mit Pipe als Trenner — nicht das übliche UTF-8/Komma."""
    r = hole(f"{SH_BASIS}/{name}/aktuell.csv")
    text = r.content.decode("iso-8859-1", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="|", quotechar='"'))


def konnektor_sh() -> list[dict]:
    """
    [PRUEF] Schleswig-Holstein, ~330 Badestellen.

    ACHTUNG: Das Portal steht hinter Anubis (Proof-of-Work-Bot-Schutz). Die
    HTML-Seiten liefern "Access Denied"; ob die /collection/*.csv-Endpunkte
    ausgenommen sind, muss der erste Lauf zeigen. Falls nicht: Betreiber wegen
    User-Agent-Whitelisting anschreiben (Argument: EU-VO 2023/138, HVD Umwelt).

    Der Name der Messwert-Collection ist noch nicht bestätigt. Kandidaten:
    badegewasser-messwerte / badegewasser-untersuchungsergebnisse.
    """
    stamm = {r["BADEGEWAESSERID"]: r for r in _sh_csv("badegewasser-stammdaten")}

    einst = {}
    try:
        for r in _sh_csv("badegewasser-einstufung"):
            einst[r["BADEGEWAESSERID"]] = r
    except Exception:
        pass

    mess = {}
    for kandidat in ("badegewasser-messwerte", "badegewasser-untersuchungsergebnisse"):
        try:
            for r in _sh_csv(kandidat):
                mess.setdefault(r["BADEGEWAESSERID"], []).append(r)
            break
        except Exception:
            continue

    def zahl(v):
        try:
            return float(str(v).replace(",", ".").replace("<", "").strip())
        except (TypeError, ValueError):
            return None

    out = []
    for sid, s in stamm.items():
        letzte = None
        if sid in mess:
            letzte = sorted(mess[sid], key=lambda r: r.get("DATUM", ""))[-1]

        art = (s.get("BADEGEWAESSERTYP") or "").lower()
        out.append(stelle(
            "SH", sid, s.get("BADEGEWAESSERNAME") or s.get("KURZNAME") or sid,
            gewaesser=s.get("GEWAESSERNAME"),
            ort=s.get("GEMEINDENAME"),
            typ="kueste" if "küste" in art or "kueste" in art else "binnen",
            lat=zahl(s.get("GEOGR_BREITE")), lon=zahl(s.get("GEOGR_LAENGE")),
            vertrauen="berechnet" if letzte else "amtlich",
            einstufung=(einst.get(sid) or {}).get("EINSTUFUNG"),
            ampel="unbekannt",
            probe=(letzte or {}).get("DATUM"),
            ecoli=zahl((letzte or {}).get("ECOLI")),
            entero=zahl((letzte or {}).get("ENTEROKOKKEN")),
            sichttiefe=zahl((letzte or {}).get("SICHTTIEFE")),
            temperatur=zahl((letzte or {}).get("WASSERTEMPERATUR")),
            url="https://www.schleswig-holstein.de/DE/landesregierung/themen/"
                "gesundheit-verbraucherschutz/badegewaesserqualitaet",
        ))
    return out


HH_WFS = "https://gateway.hamburg.de/OGCFassade/BSU_WFS_BADEGEWAESSER.aspx"


def konnektor_hh() -> list[dict]:
    """
    [PRUEF] Hamburg, WFS, Lizenz DL-DE-BY 2.0.

    Hamburg stellt Geodaten zunehmend auch über OGC API - Features bereit.
    Falls für Badegewässer verfügbar, ist das der bequemere Weg (JSON statt GML)
    und dieser Konnektor kann stark schrumpfen. Im Transparenzportal prüfen.
    """
    r = hole(HH_WFS, params={
        "SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature",
        "TYPENAME": "badegewaesser", "SRSNAME": "EPSG:4326",
    })
    wurzel = ET.fromstring(r.content)
    ns = {"gml": "http://www.opengis.net/gml"}

    out = []
    for i, member in enumerate(wurzel.iter()):
        tag = member.tag.split("}")[-1].lower()
        if "badegewaesser" not in tag or member is wurzel:
            continue
        felder = {k.tag.split("}")[-1]: (k.text or "").strip() for k in member}
        pos = member.find(".//gml:pos", ns)
        lat = lon = None
        if pos is not None and pos.text:
            teile = pos.text.split()
            if len(teile) >= 2:
                lat, lon = float(teile[0]), float(teile[1])

        out.append(stelle(
            "HH",
            felder.get("badegewaesserid") or f"DEHH_{i:04d}",
            felder.get("name") or felder.get("bezeichnung") or "Badegewässer",
            gewaesser=felder.get("gewaesser"),
            ort=felder.get("bezirk"),
            lat=lat, lon=lon,
            vertrauen="amtlich",
            einstufung=felder.get("einstufung") or felder.get("qualitaet"),
            url="https://www.hamburg.de/badegewaesser/",
        ))
    return out


MV_WFS = "https://www.geodaten-mv.de/dienste/badewassermv_wfs"


def konnektor_mv() -> list[dict]:
    """
    [PRUEF] Mecklenburg-Vorpommern, ~500 Badegewässer. WFS 2.0.0.
    Lizenz laut Metadatensatz: "Es gelten keine Bedingungen" — die freieste
    Lage aller Länder.

    Der WFS liefert vermutlich nur Geometrie und Einstufung. Für die Keimzahlen
    der laufenden Saison zusätzlich badewasser-mv.de anzapfen (siehe TODO unten).
    """
    r = hole(MV_WFS, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": "badestellen", "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
        "OUTPUTFORMAT": "application/gml+xml; version=3.2",
    })
    wurzel = ET.fromstring(r.content)
    ns = {"gml": "http://www.opengis.net/gml/3.2"}

    out = []
    for i, m in enumerate(wurzel.iter()):
        if "badestelle" not in m.tag.split("}")[-1].lower():
            continue
        f = {k.tag.split("}")[-1].lower(): (k.text or "").strip() for k in m}
        pos = m.find(".//gml:pos", ns)
        lat = lon = None
        if pos is not None and pos.text:
            t = pos.text.split()
            if len(t) >= 2:
                lat, lon = float(t[0]), float(t[1])

        name = f.get("name") or f.get("bezeichnung") or f"Badestelle {i}"
        gew = f.get("gewaesser", "")
        out.append(stelle(
            "MV", f.get("badegewaesserid") or f"DEMV_{i:04d}", name,
            gewaesser=gew, ort=f.get("gemeinde"),
            typ="kueste" if "ostsee" in gew.lower() else "binnen",
            lat=lat, lon=lon,
            vertrauen="amtlich",
            einstufung=f.get("einstufung") or f.get("qualitaet"),
            url="https://www.badewasser-mv.de/",
        ))
    return out


ST_REST = ("https://www.geodatenportal.sachsen-anhalt.de/arcgis/rest/services/"
           "LAV/Badegewaesser_LSA/MapServer")


def konnektor_st() -> list[dict]:
    """
    [PRUEF] Sachsen-Anhalt. Der dokumentierte WMS liegt unter
    /arcgis/services/LAV/Badegewaesser_LSA/MapServer/WMSServer — derselbe
    Dienst ist bei ArcGIS fast immer auch unter /arcgis/rest/services/... als
    REST-Endpunkt erreichbar. Laut Metadatensatz enthält er neueste
    Untersuchungsergebnisse, letzte Qualitätseinstufung und aktuelle Hinweise,
    also genau das gesuchte Feld-Set.
    """
    r = hole(f"{ST_REST}/0/query", params={
        "where": "1=1", "outFields": "*", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson",
    })
    gj = r.json()

    out = []
    for i, feat in enumerate(gj.get("features", [])):
        p = {k.lower(): v for k, v in (feat.get("properties") or {}).items()}
        c = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        hinweise = []
        if p.get("hinweis"):
            hinweise.append({"art": "zustand", "text": str(p["hinweis"])})

        out.append(stelle(
            "ST", p.get("badegewaesserid") or f"DEST_{i:04d}",
            p.get("name") or p.get("badegewaesser") or f"Badegewässer {i}",
            gewaesser=p.get("gewaesser"), ort=p.get("gemeinde"),
            lat=c[1], lon=c[0],
            vertrauen="amtlich",
            einstufung=p.get("einstufung"),
            probe=p.get("probedatum") or p.get("datum"),
            hinweise=hinweise,
            url="https://www.geodatenportal.sachsen-anhalt.de/mapapps/"
                "resources/apps/badegewaesserkarte/index.html",
        ))
    return out


# ==========================================================================
# BE / BB — bestehende Konnektoren einhängen
# ==========================================================================

def konnektor_be() -> list[dict]:
    """
    [TODO] Berlin, Lageso-CSV je Badestelle mit allen Messwerten der Saison.
    Hier den bereits funktionierenden Code aus der Vorgänger-App einsetzen und
    das Ergebnis auf stelle() abbilden — vertrauen="berechnet", weil E. coli,
    Enterokokken, Chlorophyll a, Sichttiefe und coliforme Bakterien vorliegen.
    """
    raise NotImplementedError("Bestehenden Lageso-Konnektor hier einhängen")


def konnektor_bb() -> list[dict]:
    """
    [TODO] Brandenburg, KML-Export des LAVG: Messdatum, Wassertemperatur,
    Sichttiefe, Bemerkung, fünfstufige Beurteilung — aber keine Keimzahlen.
    Deshalb vertrauen="amtlich" und url= auf die Detailseite setzen.
    """
    raise NotImplementedError("Bestehenden LAVG-Konnektor hier einhängen")


# ==========================================================================
# KATEGORIE B / C — Portale bekannt, Endpunkt noch zu ermitteln
# ==========================================================================

def konnektor_bw() -> list[dict]:
    """
    [TODO] badegewaesserkarte.landbw.de
    Der URL-Parameter ?data_id=dataSource_17-Gesamt_UVB_BGW_2577:257 ist die
    Signatur von ArcGIS Experience Builder. Netzwerk-Tab öffnen, XHR-Requests
    mitlesen -> darunter liegt ein FeatureServer mit /query?f=geojson.
    Dann konnektor_st() als Vorlage kopieren.
    """
    raise NotImplementedError("FeatureServer-URL aus dem Netzwerk-Tab ziehen")


def konnektor_nw() -> list[dict]:
    """
    [TODO] db.badegewaesser.nrw.de/badegewaesser-nrw/ (LANUK).
    85 EU-Badegewässer mit 111 Badestellen, aktuelle und historische Messwerte
    je Messstelle. Die Tabellenansicht ist der beste Ansatzpunkt.

    WICHTIG: NRW löst an Flussbadestellen bei einer Tagesniederschlagssumme
    über 5 mm/d automatisch ein Badeverbot aus. Dieses Frühwarn-Flag unbedingt
    als hinweise=[{"art":"warnung",...}] mitführen — es ändert sich zwischen
    zwei Beprobungen und ist der einzige tagesaktuelle Wert im Land.
    """
    raise NotImplementedError("Tabellenansicht auswerten")


def konnektor_ni() -> list[dict]:
    """
    [TODO] apps.nlga.niedersachsen.de/batlas/ — das beste Scraping-Ziel.
    URL-Schema ist vollständig deterministisch:
        index.php?p=sa                 Liste aller Badestellen (IDs holen)
        index.php?p=bm&b=<EU-ID>       Messdaten
        index.php?p=bw&b=<EU-ID>       Profil
    Kein Session-Handling, kein JS nötig.

    Vorher den Menüpunkt "Downloads" im batlas prüfen — evtl. liegen die Daten
    dort fertig als Datei und der Scraper erübrigt sich.

    Rechtlich: Messwerte sind Fakten und unproblematisch. Fotos und
    Profiltexte NICHT übernehmen, die stehen unter "Alle Rechte vorbehalten".
    """
    raise NotImplementedError("batlas-Scraper bauen")


def konnektor_rp() -> list[dict]:
    """[TODO] badeseen.rlp.de (LfU). Saison nur 1.6.–31.8., keine Fließgewässer."""
    raise NotImplementedError()


def konnektor_he() -> list[dict]:
    """
    [TODO] badeseen.hlnug.de.
    Zuerst das HLNUG-Dienstverzeichnis prüfen (hlnug.de/themen/wasser/daten-und-viewer):
    liegt dort ein Badegewässer-WFS, wird Hessen von Kategorie C nach A gehoben.
    """
    raise NotImplementedError()


def konnektor_sn() -> list[dict]:
    """
    [TODO] gesunde.sachsen.de/badegewaesser.html.
    Alternativ LUIS (luis.sachsen.de) prüfen — das LfULG bietet dort neben
    WMS/WFS ausdrücklich auch REST-Feature-Services an.
    """
    raise NotImplementedError()


def konnektor_th() -> list[dict]:
    """[TODO] verbraucherschutz.thueringen.de/gesundheit/badegewaesser. Nur 38 Badestellen."""
    raise NotImplementedError()


def konnektor_hb() -> list[dict]:
    """
    [TODO] umwelt.bremen.de + transparenz.bremen.de.
    10 Badeseen + 1 Weser-Badestelle, Wassertemperatur wöchentlich.
    Dunger See, Grambker Feldmarksee und Kuhgrabensee sind aus Naturschutz-
    gründen grundsätzlich gesperrt -> als Warnhinweis setzen.
    Bremerhaven ist hier NICHT enthalten, separat prüfen.
    """
    raise NotImplementedError()


def konnektor_sl() -> list[dict]:
    """
    [ENTFÄLLT] Saarland veröffentlicht nur PDFs, und es geht um 3 Badegewässer
    mit 5 Stränden. Ein PDF-Parser lohnt nicht. Entweder von Hand in
    data/manuell/sl.json pflegen oder bei der EEA-Jahresnote belassen.
    """
    pfad = os.path.join(os.path.dirname(__file__), "..", "data", "manuell", "sl.json")
    if os.path.exists(pfad):
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    return []


def konnektor_by() -> list[dict]:
    """
    [ENTFÄLLT] Bayern zentralisiert nicht. Das LGL veröffentlicht die Karte mit
    375 EU-Badestellen, verweist für die aktuellen Überwachungsergebnisse aber
    auf die zuständigen Kreisverwaltungsbehörden — bis zu 96 verschiedene
    Websites ohne gemeinsames Format.

    Bayern bleibt deshalb bei der EU-Jahresnote aus dem EEA-Layer plus Deeplink.
    Das ist dieselbe Darstellungsform wie bei Brandenburg und braucht kein
    neues UI-Konzept.

    Besserer Hebel als 96 Scraper: das LGL anschreiben. Argument ist die
    EU-Durchführungsverordnung 2023/138 zu hochwertigen Datensätzen, Kategorie
    Umwelt — sie verpflichtet zur maschinenlesbaren Bereitstellung bereits
    erhobener Daten, möglichst über APIs.
    """
    return []


KONNEKTOREN: dict[str, Callable[[], list[dict]]] = {
    "SH": konnektor_sh, "HH": konnektor_hh, "MV": konnektor_mv, "ST": konnektor_st,
    "BE": konnektor_be, "BB": konnektor_bb,
    "BW": konnektor_bw, "NW": konnektor_nw, "NI": konnektor_ni, "RP": konnektor_rp,
    "HE": konnektor_he, "SN": konnektor_sn, "TH": konnektor_th, "HB": konnektor_hb,
    "SL": konnektor_sl, "BY": konnektor_by,
}


# --------------------------------------------------------------------------
# Zusammenführen
# --------------------------------------------------------------------------

def verschmelze(basis: dict[str, dict], neu: list[dict]) -> None:
    """Länderdaten reichern die EEA-Basis an. Gesetzt wird nur, was befüllt ist."""
    for s in neu:
        alt = basis.get(s["id"])
        if not alt:
            basis[s["id"]] = s
            continue
        for k, v in s.items():
            if k == "messwerte":
                for mk, mv in v.items():
                    if mv is not None:
                        alt["messwerte"][mk] = mv
            elif k == "hinweise":
                if v:
                    alt["hinweise"] = v
            elif v not in (None, "", "unbekannt", "jahresnote"):
                alt[k] = v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/badestellen.json")
    ap.add_argument("--nur", default="", help="Kommaliste von Länderkürzeln")
    ap.add_argument("--status", action="store_true", help="nur prüfen, nichts schreiben")
    args = ap.parse_args()

    gewuenscht = [x.strip().upper() for x in args.nur.split(",") if x.strip()] or LAENDER
    quellen: dict[str, dict] = {}

    # bisherigen Stand laden, um bei Ausfällen darauf zurückzufallen
    vorher: dict[str, dict] = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                alt = json.load(f)
            vorher = {s["id"]: s for s in alt.get("stellen", [])}
            print(f"  bestehender Stand: {len(vorher)} Stellen")
        except Exception as e:
            print(f"  bestehende Datei nicht lesbar: {e}")

    basis: dict[str, dict] = {}

    print("EEA-Basis …", flush=True)
    try:
        eea = konnektor_eea()
        basis = {s["id"]: s for s in eea}
        quellen["EEA"] = {"ok": True, "n": len(eea), "stand": datetime.now(timezone.utc).isoformat()}
        print(f"  {len(eea)} Stellen")
    except Exception as e:
        quellen["EEA"] = {"ok": False, "fehler": str(e)}
        print(f"  FEHLER: {e}")
        if vorher:
            basis = dict(vorher)
            print(f"  weiter mit dem letzten Stand ({len(basis)} Stellen)")

    for land in gewuenscht:
        fn = KONNEKTOREN.get(land)
        if not fn:
            continue
        print(f"{land} …", end=" ", flush=True)
        try:
            daten = fn()
            verschmelze(basis, daten)
            quellen[land] = {"ok": True, "n": len(daten),
                             "stand": datetime.now(timezone.utc).isoformat()}
            print(f"{len(daten)} Stellen")
        except NotImplementedError as e:
            quellen[land] = {"ok": False, "grund": "Konnektor fehlt", "hinweis": str(e)}
            print("Konnektor fehlt — EEA-Jahresnote bleibt stehen")
        except Exception as e:
            quellen[land] = {"ok": False, "fehler": str(e)}
            print(f"FEHLER: {e}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()

    if args.status:
        print(json.dumps(quellen, indent=2, ensure_ascii=False))
        return 0

    stellen = sorted(basis.values(), key=lambda s: (s["land"], s["name"] or ""))
    ausgabe = {
        "stand": datetime.now(timezone.utc).isoformat(),
        "demo": False,
        "quellen": quellen,
        "stellen": stellen,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ausgabe, f, ensure_ascii=False, separators=(",", ":"))

    kb = os.path.getsize(args.out) / 1024
    ok = sum(1 for q in quellen.values() if q.get("ok"))
    print(f"\n{len(stellen)} Stellen aus {ok}/{len(quellen)} Quellen -> {args.out} ({kb:.0f} kB)")

    if not stellen:
        print("Keine Daten. Bestehende Datei wurde nicht überschrieben.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
