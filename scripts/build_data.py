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
import html
import json
import math
import os
import re
import sys
import time
import traceback
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

EEA_BASE = "https://marine.discomap.eea.europa.eu/arcgis/rest/services/BathingWater"


def _eea_service_url() -> str:
    """Die EEA veröffentlicht jedes Berichtsjahr einen neu datierten Dienst
    (…_Dyna_WM_2025 usw.) und lässt die älteren stehen. Der undatierte
    "BathingWater_Dyna_WM" ist KEIN Alias auf den neuesten Stand, sondern
    selbst eingefroren (Stand Juli 2026 bei Berichtsjahr 2022). Deshalb den
    höchsten Jahrgang zur Laufzeit ermitteln statt ein Jahr fest zu verdrahten."""
    r = hole(f"{EEA_BASE}?f=json")
    namen = [s["name"].rsplit("/", 1)[-1] for s in r.json().get("services", [])]
    jahre = []
    for n in namen:
        m = re.fullmatch(r"BathingWater_Dyna_WM_(\d{4})", n)
        if m:
            jahre.append(int(m.group(1)))
    if not jahre:
        raise RuntimeError("Kein datierter EEA-Dienst gefunden (BathingWater_Dyna_WM_<Jahr>)")
    return f"{EEA_BASE}/BathingWater_Dyna_WM_{max(jahre)}/MapServer"


def _eea_find_point_layer(service_url: str) -> int:
    """Die Layer-ID des Punkt-Layers wechselt zwischen Jahrgängen (2018er Dienst:
    ID 0-4 alle "Bathing water quality"; 2025er Dienst: ID 3 "…(point)", ID 14
    "…(symbol)"). Über geometryType und Namen auflösen statt eine ID zu raten."""
    layers = hole(f"{service_url}?f=json").json().get("layers", [])
    punkte = [l for l in layers if l.get("geometryType") == "esriGeometryPoint"]
    if not punkte:
        raise RuntimeError("Kein Punkt-Layer im EEA-Dienst gefunden")
    bevorzugt = [l for l in punkte if "point" in l["name"].lower()]
    return (bevorzugt or punkte)[0]["id"]


def konnektor_eea() -> list[dict]:
    """
    Basis für alle 16 Länder. EEA-ArcGIS-REST, aktuellster jährlicher Dienst
    (siehe _eea_service_url). Liefert Name, Koordinaten, Wasserart, EU-Jahres-
    einstufung und Link auf das nationale Badegewässerprofil. Enthält KEINE
    Gewässer- oder Gemeindenamen — die liefern ggf. die Länderkonnektoren.

    Feldnamen bestätigt am Dienst BathingWater_Dyna_WM_2025, Layer
    "Bathing water quality (point)" (Stand Juli 2026):
    bathingWaterIdentifier, bathingWaterName, countryCode, bwWaterCategory
    (Coastal|Transitional|Lake|River), qualityStatus (Excellent|Good|
    Sufficient|Poor|Not classified), bwProfileLink, longitude, latitude.
    """
    service = _eea_service_url()
    layer_id = _eea_find_point_layer(service)

    params = {
        "where": "countryCode='DE'",
        "outFields": "bathingWaterIdentifier,bathingWaterName,bwWaterCategory,"
                     "qualityStatus,bwProfileLink,longitude,latitude",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": 2000,
    }

    stellen, offset = [], 0
    while True:
        params["resultOffset"] = offset
        r = hole(f"{service}/{layer_id}/query", params=params)
        gj = r.json()
        feats = gj.get("features", [])
        if not feats:
            break

        for f in feats:
            p = f.get("properties") or {}
            geom = (f.get("geometry") or {}).get("coordinates") or [None, None]

            bwid = p.get("bathingWaterIdentifier")
            if not bwid:
                continue
            land = EU_PREFIX.get(str(bwid)[:4].upper())
            if not land:
                continue

            art = (p.get("bwWaterCategory") or "").lower()
            typ = "kueste" if art in ("coastal", "transitional") else "binnen"
            eins = p.get("qualityStatus")
            if eins == "Not classified":
                eins = None

            stellen.append(stelle(
                land, str(bwid), p.get("bathingWaterName") or str(bwid),
                typ=typ,
                lat=geom[1], lon=geom[0],
                vertrauen="jahresnote",
                einstufung=eins,
                ampel=einstufung_ampel(eins),
                url=p.get("bwProfileLink"),
            ))

        if len(feats) < params["resultRecordCount"]:
            break
        offset += len(feats)
        time.sleep(0.4)

    return stellen


# ==========================================================================
# KATEGORIE A — echte Schnittstellen mit Messwerten
# ==========================================================================

SH_QUELLEN = {
    "stammdaten": "https://efi2.schleswig-holstein.de/bg/opendata/v_badegewaesser_odata.csv",
    "einstufung": "https://efi2.schleswig-holstein.de/bg/opendata/v_einstufung_odata.csv",
    "messungen": "https://efi2.schleswig-holstein.de/bg/opendata/v_proben_odata.csv",
}


def _sh_zeilen(url: str) -> list[list[str]]:
    """Rohdaten des Landes: ISO-8859-1, Pipe-getrennt, KEINE Kopfzeile.

    Die CKAN-gehostete Kopie unter opendata.schleswig-holstein.de/collection/…
    hat beim Archivieren alle Umlaute durch U+FFFD ersetzt (nachgeprüft:
    Bytefolge EF BF BD an jeder Umlautstelle, unabhängig vom Decoder — die
    Zeichen sind an der Quelle schon weg). efi2.schleswig-holstein.de ist der
    Originaldienst des Sozialministeriums dahinter: sauberes ISO-8859-1,
    Umlaute intakt, tagesaktuell. Kein Anubis-Block auf diesem Pfad."""
    r = hole(url)
    text = r.content.decode("iso-8859-1", errors="replace")
    return [z.split("|") for z in text.splitlines() if z.strip()]


def konnektor_sh() -> list[dict]:
    """
    Schleswig-Holstein, ~330 Badestellen.

    Spaltennamen stehen in keiner der drei CSV-Dateien selbst (keine
    Kopfzeile), sondern nur in der CKAN-Datensatzbeschreibung
    (package_show.notes). Reihenfolge Stand Juli 2026, Position für Position
    an Beispieldaten verifiziert:

    Stammdaten (29 Spalten, hier nur die verwendeten mit Index):
      0 BADEGEWAESSERID  3 ALLGEMEIN_GEBRAEUCHL_NAME  4 GEWAESSERKATEGORIE
      14 WASSERKOERPERNAME  21 GEMEINDE  24 GEOGR_LAENGE  25 GEOGR_BREITE
    (Index 1 BADEGEWAESSERNAME ist der amtliche Name in GROSSSCHREIBUNG mit
    Semikolons, Index 3 der im Alltag gebräuchliche — deshalb Index 3 für die
    Anzeige.)

    Einstufung (4 Spalten): BADEGEWAESSERID, BEURTEILUNGSZEITRAUM_VON,
    BEURTEILUNGSZEITRAUM_BIS, EINSTUFUNG_ODER_VORABBEWERTUNG. Mehrere Zeilen
    je Badestelle (ein Berichtszeitraum pro Zeile) — es zählt die mit dem
    höchsten BEURTEILUNGSZEITRAUM_BIS.

    Messungen (16 Spalten, verwendet: 0 BADEGEWAESSERID, 8 DATUMMESSUNG,
    10 ECOLI, 11 INTEST_ENTEROKOKKEN, 12 WASSERTEMP, 14 SICHTTIEFE). Manche
    Badestellen haben mehrere Messstellen (20 von 330); hier zählt schlicht
    die zeitlich letzte Messung über alle Messstellen der Badestelle hinweg.

    url wird bewusst nicht gesetzt: Die EEA-Basis liefert bereits einen
    Deeplink auf die offizielle Landeskarte je Badestelle (…DarstellungBade-
    stelle.html#bgst=<ID>), der genauer ist als ein pauschaler Themenseiten-
    Link und sonst von verschmelze() überschrieben würde.
    """
    stamm = {r[0]: r for r in _sh_zeilen(SH_QUELLEN["stammdaten"]) if r and r[0]}

    neuste_einstufung: dict[str, tuple[str, str]] = {}
    for r in _sh_zeilen(SH_QUELLEN["einstufung"]):
        if len(r) < 4 or not r[0]:
            continue
        sid, bis, text = r[0], r[2].strip(), r[3].strip()
        bisher = neuste_einstufung.get(sid)
        if not bisher or bis > bisher[0]:
            neuste_einstufung[sid] = (bis, text)

    def zahl(v):
        try:
            return float(str(v).replace(",", ".").replace("<", "").strip())
        except (TypeError, ValueError):
            return None

    def probedatum(v: str) -> str | None:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v or "")
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    neuste_probe: dict[str, tuple[str, list[str]]] = {}
    for r in _sh_zeilen(SH_QUELLEN["messungen"]):
        if len(r) < 16 or not r[0]:
            continue
        datum = probedatum(r[8])
        if not datum:
            continue
        bisher = neuste_probe.get(r[0])
        if not bisher or datum > bisher[0]:
            neuste_probe[r[0]] = (datum, r)

    out = []
    for sid, s in stamm.items():
        kategorie = s[4].strip()
        probe = neuste_probe.get(sid)
        eins = neuste_einstufung.get(sid)
        ecoli = zahl(probe[1][10]) if probe else None
        entero = zahl(probe[1][11]) if probe else None

        out.append(stelle(
            "SH", sid, s[3].strip() or s[1].strip() or s[2].strip() or sid,
            gewaesser=s[14].strip() or None,
            ort=s[21].strip() or None,
            typ="kueste" if kategorie in ("Küstengewässer", "Übergangsgewässer") else "binnen",
            lat=zahl(s[25]), lon=zahl(s[24]),
            vertrauen="berechnet" if (ecoli is not None and entero is not None)
                      else ("amtlich" if eins else "jahresnote"),
            einstufung=eins[1] if eins else None,
            ampel=einstufung_ampel(eins[1] if eins else None),
            probe=probe[0] if probe else None,
            ecoli=ecoli, entero=entero,
            temperatur=zahl(probe[1][12]) if probe else None,
            sichttiefe=zahl(probe[1][14]) if probe else None,
        ))
    return out


HH_WFS = "https://geodienste.hamburg.de/HH_WFS_Badegewaesser"

# EU-Jahreseinstufung, Stand Juli 2026 nur mit den Codes 2 und 3 in freier Wildbahn
# gesehen; als Standardskala des LGV übernommen. Codes <=0 kommen ebenfalls vor
# (z.B. -1 bei "gesperrt", -2 bei einer Stelle mit "keine Beanstandung") und lassen
# sich ohne offizielle Dokumentation nicht sicher deuten - bewusst nicht gemappt,
# um keine Einstufung zu erfinden. Das eigentliche Warnsignal liefert "hinweis".
HH_EINSTUFUNG = {4: "ausgezeichnet", 3: "gut", 2: "ausreichend", 1: "mangelhaft"}


def _hh_geojson(typename: str) -> list[dict]:
    r = hole(HH_WFS, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": typename, "OUTPUTFORMAT": "application/geo+json",
        "SRSNAME": "EPSG:4326",
    })
    return r.json().get("features", [])


def _hh_eea_referenz() -> list[tuple[str, float, float, str]]:
    """Die WFS-Stammdaten haben keine BADEGEWAESSERID, nur einen Namen, der
    nicht mit dem EEA-Namen übereinstimmt ("Eichbaumsee, Badestelle Nord" vs.
    "EICHBAUMSEE BADEPLATZ NORD"). Für die amtliche ID und den Wasserart-Code
    daher die EEA-Basis auf HH gefiltert erneut abfragen und über die
    Koordinate zuordnen (siehe _naechste_id) - kein Raten, alle 17 Hamburger
    Stellen liegen unter 260 m vom EEA-Punkt entfernt, die zweitnächste
    Kandidatin jeweils über 600 m."""
    service = _eea_service_url()
    layer_id = _eea_find_point_layer(service)
    r = hole(f"{service}/{layer_id}/query", params={
        "where": "countryCode='DE' AND bathingWaterIdentifier LIKE 'DEHH%'",
        "outFields": "bathingWaterIdentifier,bwWaterCategory,longitude,latitude",
        "returnGeometry": "false", "f": "json", "resultRecordCount": 200,
    })
    return [(a["bathingWaterIdentifier"], a["latitude"], a["longitude"], a.get("bwWaterCategory") or "")
            for a in (f["attributes"] for f in r.json().get("features", []))]


def _naechste_id(lat: float, lon: float, kandidaten: list[tuple[str, float, float, str]],
                  max_m: float = 500.0):
    def abstand_m(lat1, lon1, lat2, lon2):
        R = 6371000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    beste = min(kandidaten, key=lambda k: abstand_m(lat, lon, k[1], k[2]))
    return beste if abstand_m(lat, lon, beste[1], beste[2]) <= max_m else None


def konnektor_hh() -> list[dict]:
    """
    Hamburg, 17 Badestellen. WFS 2.0.0, Feature-Typen app:badegewaesser
    (Stammdaten) und app:badegewaesser_proben (Einzelmessungen der Saison,
    verknüpft über den Namen - beide Feature-Typen stammen aus demselben
    Fachverfahren und benutzen identische Namen).

    badegewaesser_proben-Felder: name, datum (JJJJMMTT), mpn (E. coli nach der
    MPN-Methode, DIN EN ISO 9308-3 - die für Hamburg dokumentierte Methode für
    genau diesen Parameter), enterokokken, temperatur, sichttiefe (alle mit
    Dezimalkomma, teils "<15"/">2,0" zensiert).

    hinweis ist NNNN (kein Hinweis) oder baden_verboten - das ist das
    verlässliche Warnsignal, nicht eg_einstufung (siehe HH_EINSTUFUNG oben).

    url kommt aus link_zu_details (Stellen-Steckbrief mit aktuellem Status);
    das ersetzt bewusst den generischen EEA-Link auf ein PDF-Jahresprofil.
    """
    referenz = _hh_eea_referenz()

    proben_je_name: dict[str, list[str]] = {}
    for f in _hh_geojson("app:badegewaesser_proben"):
        p = f["properties"]
        name = p.get("name")
        if name:
            proben_je_name.setdefault(name, []).append(p)

    def zahl(v):
        if v is None:
            return None
        try:
            return float(str(v).replace(",", ".").replace("<", "").replace(">", "").strip())
        except (TypeError, ValueError):
            return None

    out = []
    for f in _hh_geojson("app:badegewaesser"):
        p = f["properties"]
        name = p.get("name")
        geom = f.get("geometry") or {}
        lon, lat = (geom.get("coordinates") or [None, None])[:2]
        if lat is None or lon is None or not name:
            continue

        treffer = _naechste_id(lat, lon, referenz)
        if not treffer:
            continue
        bwid, _, _, kategorie = treffer

        proben = proben_je_name.get(name) or []
        letzte = sorted(proben, key=lambda r: r.get("datum") or "")[-1] if proben else None

        hinweise = []
        if p.get("hinweis") == "baden_verboten" or p.get("badesaison") == "gesperrt":
            hinweise.append({"art": "warnung", "text": p.get("bemerkung_bsu") or "Baden verboten"})

        ecoli = zahl((letzte or {}).get("mpn"))
        entero = zahl((letzte or {}).get("enterokokken"))

        out.append(stelle(
            "HH", bwid, name,
            ort="Hamburg",
            typ="kueste" if kategorie in ("Coastal", "Transitional") else "binnen",
            lat=lat, lon=lon,
            vertrauen="berechnet" if (ecoli is not None and entero is not None) else "amtlich",
            einstufung=HH_EINSTUFUNG.get(p.get("eg_einstufung")),
            ampel=einstufung_ampel(HH_EINSTUFUNG.get(p.get("eg_einstufung"))),
            probe=(f"{letzte['datum'][:4]}-{letzte['datum'][4:6]}-{letzte['datum'][6:8]}"
                   if letzte and letzte.get("datum") else None),
            ecoli=ecoli, entero=entero,
            temperatur=zahl((letzte or {}).get("temperatur")),
            sichttiefe=zahl((letzte or {}).get("sichttiefe")),
            hinweise=hinweise,
            url=p.get("link_zu_details"),
        ))
    return out


def konnektor_mv() -> list[dict]:
    """
    [TODO] Mecklenburg-Vorpommern — größer als "nur Feldnamen korrigieren",
    braucht einen eigenen Scraper wie NI. Kein Raten mehr nötig, der Weg ist
    klar, nur der Aufwand sprengt diesen Durchgang:

    Geprüft und bestätigt per echtem Abruf (nicht geraten):

    1. Der WFS (sm:badewassermv_wfs, EPSG:25833, GeoJSON via
       OUTPUTFORMAT=application/json; subtype=geojson) liefert NUR
       {name, eu: "ja"/"nein", anzeige_nr} + Geometrie. 546 Feature, davon 305
       mit eu=ja. Keine Einstufung, keine Keimzahlen — der alte Docstring hat
       das falsch vermutet. Dieser Dienst allein bringt gegenüber der
       EEA-Basis keinen Mehrwert (die hat Name+Koordinaten schon).

    2. Die echten Messwerte (E. coli, Intestinale Enterokokken, Wassertemperatur,
       Sichttiefe, pH, Datum) stehen serverseitig gerendert in einer Tabelle
       (class="badestelle-messwerte") auf:
         https://www.regierung-mv.de/Landesregierung/sm/gesundheit/
         Badewasserqualitaet/badewasserkarte/badestelle?gaia.badestelle.id=<ID>
       Eine Seite pro Badestelle, kein Login, kein Anubis-Block (getestet mit
       ID 558 = Schmaler Luzin, Carwitz: 3 Messungen Mai-Juli 2026 abrufbar).

    3. Die ID-Liste dafür liefert badewasser-mv.de selbst in zwei Bulk-JSON-
       Feeds (im Netzwerk-Tab gefunden, kein Login, kein Rate-Limit sichtbar):
         https://badewasser-mv.de/_badestelle.php       — alle Stellen,
           Feld "public_id" ist genau die gaia.badestelle.id von oben.
           Koordinaten in EPSG:25832, brauchen Umrechnung nach WGS84.
         https://badewasser-mv.de/_warnhinweise.php     — aktuelle Warnungen
           (z.B. Cyanobakterien/Blaualgen) im Klartext, direkt als
           hinweise=[{"art":"warnung",...}] verwendbar, keine Einzelabrufe
           nötig.

    4. public_id/id sind KEINE amtlichen BADEGEWAESSERID (Format DEMV_...) —
       Zuordnung zur EEA-Basis müsste wie bei HH über Koordinaten-Abstand
       laufen (~300 EU-Stellen für konnektor_eea() relevant).

    Damit ist der Umfang klar: ~300 Einzelseiten scrapen + Koordinaten-Join,
    nicht eine Codezeile Feldname. Das ist ein eigener Durchgang wert (siehe
    konnektor_ni() als Vorlage für den Scraper-Stil), nicht dieser.
    """
    raise NotImplementedError(
        "WFS liefert keine Messwerte; echte Daten müssen pro Stelle von "
        "regierung-mv.de gescrapt werden (Details im Docstring) — eigener "
        "Durchgang, kein Feldnamen-Fix."
    )


ST_REST = ("https://www.geodatenportal.sachsen-anhalt.de/arcgis/rest/services/"
           "LAV/Badegewaesser_LSA/MapServer")


def konnektor_st() -> list[dict]:
    """
    Sachsen-Anhalt, 70 Badestellen. Layer-Index war falsch geraten (0 statt 1
    - ArcGIS-Layer-IDs sind hier NICHT lückenlos ab 0 durchnummeriert: 1
    "Badegewässer" (Punkte, verwendet), 2 "Badegewässerflächen ALKIS"
    (Polygone), 3 "Aktuelle Meldungen"). Feldnamen waren komplett geraten
    (badegewaesserid/name/gewaesser/gemeinde/einstufung/hinweis existieren
    nicht); echte Felder unten, bestätigt gegen die Live-Antwort.

    BGW_NR (z.B. "0033") ist NICHT die volle amtliche ID, sondern nur die
    laufende Nummer darin - zusammengesetzt wird DEST_PR_<BGW_NR>, geprüft
    gegen die EEA-Basis (BGW_NR 0033 == DEST_PR_0033 == "Kulk Gommern").

    WARNUNG ist "Baden verboten" (echtes Badeverbot, z.B. Blaualgen) oder
    "Information" (schwächerer Hinweis) oder leer. AUFHEBUNG/AUFHEBUNG_DATUM
    werden defensiv geprüft, auch wenn im aktuellen Bestand nie befüllt -
    ein aufgehobener Hinweis soll nicht als aktive Warnung durchgereicht
    werden, falls der Dienst das Feld doch mal nutzt.
    """
    r = hole(f"{ST_REST}/1/query", params={
        "where": "1=1", "outFields": "*", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson",
    })
    gj = r.json()

    def zahl(v):
        try:
            return float(str(v).replace(",", ".").replace("<", "").strip())
        except (TypeError, ValueError):
            return None

    def probedatum(v: str) -> str | None:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v or "")
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    out = []
    for feat in gj.get("features", []):
        p = feat.get("properties") or {}
        c = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        bgw_nr = p.get("BGW_NR")
        if not bgw_nr or not p.get("GEWAESSER"):
            # Ohne Gewässernamen bleibt außer der Kreis-Zuordnung nichts übrig
            # (beobachtet bei BGW_NR 0026: alle Felder None bis auf LANDKREIS).
            # name=bgw_nr würde sonst per verschmelze() den guten EEA-Namen
            # überschreiben, weil eine nicht-leere Zeichenkette kein "leerer"
            # Wert im Sinne von verschmelze() ist.
            continue

        ecoli = zahl(p.get("ESCHERICHIA_COLI"))
        entero = zahl(p.get("ENTEROKOKKEN"))
        qualitaet = p.get("QUALITÄT")

        hinweise = []
        warnung = p.get("WARNUNG")
        if warnung and not p.get("AUFHEBUNG"):
            hinweise.append({
                "art": "warnung" if warnung == "Baden verboten" else "zustand",
                "text": p.get("BEMERKUNG_WARNUNG") or warnung,
            })

        out.append(stelle(
            "ST", f"DEST_PR_{bgw_nr}", p.get("GEWAESSER") or bgw_nr,
            ort=p.get("LANDKREIS"),
            lat=c[1], lon=c[0],
            vertrauen="berechnet" if (ecoli is not None and entero is not None)
                      else ("amtlich" if qualitaet else "jahresnote"),
            einstufung=qualitaet,
            ampel=einstufung_ampel(qualitaet),
            probe=probedatum(p.get("DATUM")),
            ecoli=ecoli, entero=entero,
            temperatur=zahl(p.get("ENTNAHMETEMPERATUR")),
            sichttiefe=zahl(p.get("SICHTTIEFE")),
            hinweise=hinweise,
            url=p.get("LINK"),
        ))
    return out


# ==========================================================================
# BE / BB — aus der Vorgänger-App (github.com/wosatex/badestellen) portiert
# ==========================================================================

BE_CSV_URL = "https://www.data.lageso.de/baden/00_History_gesamt/History.csv"
BE_SEITE = "https://www.berlin.de/lageso/gesundheit/gesundheitsschutz/badegewaesser/liste-der-badestellen/"

# Lageso liefert selbst keine Koordinaten. Kartenpunkte aus der Vorgänger-App
# (dort von Hand recherchiert) — nicht amtlich, aber die einzige verfügbare
# Ortsangabe. Kein Messwert, keine Gesundheitsdaten, daher unproblematisch.
BE_KOORDINATEN = {
    "Sandhauser Straße": (52.5578, 13.2075),
    "Bürgerablage": (52.5624, 13.213),
    "Tegeler See, Strandbad": (52.5836, 13.262),
    "Tegeler See, gegenüber Scharfenberg": (52.59, 13.2545),
    "Tegeler See, gegenüber Reiswerder": (52.5866, 13.2503),
    "Tegeler See, Saatwinkel": (52.5716, 13.2565),
    "Tegeler See, Reiherwerder": (52.5878, 13.2676),
    "Kleine Badewiese": (52.4869, 13.1907),
    "Grunewaldturm": (52.493, 13.1936),
    "Lieper Bucht": (52.4826, 13.1866),
    "Radfahrerwiese": (52.47, 13.183),
    "Breitehorn": (52.4563, 13.1747),
    "Große Steinlanke": (52.4402, 13.1781),
    "Alter Hof": (52.4341, 13.1836),
    "Wannsee, Strandbad": (52.4356, 13.1783),
    "Teufelssee": (52.4752, 13.234),
    "Krumme Lanke": (52.4404, 13.234),
    "Schlachtensee": (52.4381, 13.2132),
    "Kleiner Müggelsee": (52.4401, 13.618),
    "Müggelsee, Strandbad": (52.4342, 13.652),
    "Friedrichshagen, Strandbad": (52.4468, 13.6272),
    "Schmöckwitz": (52.3727, 13.6424),
    "Seddinsee": (52.3806, 13.6603),
    "Große Krampe": (52.4021, 13.648),
    "Bammelecke": (52.3903, 13.642),
    "Grünau, Strandbad": (52.4133, 13.5763),
    "Wendenschloss, Strandbad": (52.4328, 13.578),
    "Gartenstraße, Flussbad": (52.456, 13.582),
    "Dämeritzsee": (52.4288, 13.682),
    "Orankesee, Strandbad": (52.5528, 13.4702),
    "Weißensee, Strandbad": (52.556, 13.4642),
    "Plötzensee, Strandbad": (52.5443, 13.3352),
    "Flughafensee": (52.5718, 13.3022),
    "Jungfernheide, Strandbad": (52.539, 13.276),
    "Heiligensee, Strandbad": (52.6048, 13.2352),
    "Lübars, Strandbad (Ziegeleisee)": (52.61, 13.3618),
    "Halensee, Strandbad": (52.493, 13.29),
    "Groß Glienicker See, nördlich": (52.4702, 13.1128),
    "Groß Glienicker See, südlich": (52.4632, 13.113),
}

# Generische Wörter, die beim Namensabgleich gegen die EEA-Basis ignoriert
# werden (siehe _be_finde_id) — sonst matchen z.B. alle "Tegeler See"-Stellen
# gleich gut auf "SEE".
BE_STOPWOERTER = {"STRANDBAD", "SEE", "FREIBAD", "SEEBAD", "SEEBADEANSTALT",
                  "BADEWIESE", "UNTERHAVEL", "OBERHAVEL"}


def _normalisiere(s: str) -> set[str]:
    s = s.upper().replace("Ä", "AE").replace("Ö", "OE").replace("Ü", "UE").replace("ß", "SS")
    s = re.sub(r"[^A-Z0-9]+", " ", s).strip()
    return set(s.split())


def _be_eea_namen() -> list[tuple[str, str]]:
    service = _eea_service_url()
    layer_id = _eea_find_point_layer(service)
    r = hole(f"{service}/{layer_id}/query", params={
        "where": "countryCode='DE' AND bathingWaterIdentifier LIKE 'DEBE%'",
        "outFields": "bathingWaterIdentifier,bathingWaterName",
        "returnGeometry": "false", "f": "json", "resultRecordCount": 200,
    })
    return [(a["bathingWaterIdentifier"], a["bathingWaterName"])
            for a in (f["attributes"] for f in r.json().get("features", []))]


def _be_finde_id(name: str, kandidaten: list[tuple[str, str]]) -> str:
    ziel = _normalisiere(name)
    kern_ziel = ziel - BE_STOPWOERTER or ziel

    def punkte(eea_name: str):
        kandidat = _normalisiere(eea_name)
        kern = kandidat - BE_STOPWOERTER or kandidat
        return (len(kern_ziel & kern), len(ziel & kandidat))

    return max(kandidaten, key=lambda k: punkte(k[1]))[0]


def konnektor_be() -> list[dict]:
    """
    Berlin, 38 Badestellen. Lageso-CSV mit der gesamten Saisonhistorie, eine
    Zeile je Messung; genommen wird pro Badestelle die zeitlich letzte.

    ACHTUNG Encoding-Falle: Der Server deklariert (und requests erkennt)
    ISO-8859-1, die Bytes sind aber tatsächlich UTF-8 (geprüft: "Straße"
    kommt als 0xC3 0x9F, nicht 0xDF). r.text/r.encoding liefern deshalb
    Datenmüll ("StraÃ\\x9fe") - r.content explizit als UTF-8 dekodieren.

    Spalten (Semikolon-getrennt, 11 Stück, Kopfzeile nur zur Validierung):
    BadName;Prob_Datum;Escherichia coli;Intestinale Enterokokken;Coliforme
    Bakterien;Sichttiefe;Cyanobakterien Chl a;Wassertemperatur;Aktuelle
    Warnhinweise;Weitere Informationen;Farbe.

    Farbe ist bereits Lageso-eigene Tagesampel (gruen/gelb/rot) - direkt als
    ampel übernehmen, nicht aus einer Einstufung ableiten. Coliforme
    Bakterien haben im Datenvertrag kein Feld (nur ecoli/entero/chlorophyll/
    sichttiefe/temperatur) und werden nicht mitgeführt - das ist im
    ursprünglichen Scoring der Vorgänger-App ohnehin nur ein Nebenfaktor.

    Lageso liefert weder Koordinaten noch eine amtliche ID. Koordinaten
    kommen aus BE_KOORDINATEN (Vorgänger-App), die ID aus einem Namensabgleich
    gegen die EEA-Basis (_be_finde_id) - Koordinaten-Matching wie bei HH
    scheitert hier, weil BE_KOORDINATEN nur grobe Kartenpunkte sind, keine
    amtlichen Positionen (manche >1 km von der echten Lage entfernt).
    """
    text = hole(BE_CSV_URL).content.decode("utf-8")
    zeilen = [z for z in text.replace("\r", "").split("\n") if z.strip()]
    if not zeilen or "BadName" not in zeilen[0]:
        raise ValueError("Berlin: unerwarteter CSV-Kopf")

    def zahl(v):
        s = str(v or "").strip()
        if not s or s == "-" or re.fullmatch(r"n\.?\s?a\.?", s, re.I):
            return None
        m = re.match(r"^([<>])?\s*([\d.,]+)", s)
        if not m:
            return None
        n = re.sub(r"\.(?=\d{3}\b)", "", m.group(2)).replace(",", ".")
        try:
            return float(n)
        except ValueError:
            return None

    def probedatum(v: str) -> str | None:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v or "")
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    je_name: dict[str, list[list[str]]] = {}
    for z in zeilen[1:]:
        f = z.split(";")
        if len(f) < 11 or not probedatum(f[1]):
            continue
        je_name.setdefault(f[0].strip(), []).append(f)

    if len(je_name) < 20:
        raise ValueError(f"Berlin: nur {len(je_name)} Badestellen gelesen")

    kandidaten = _be_eea_namen()
    out = []
    for name, rows in je_name.items():
        rows.sort(key=lambda f: probedatum(f[1]))
        letzte = rows[-1]
        ecoli, entero = zahl(letzte[2]), zahl(letzte[3])
        warn, info, farbe = letzte[8].strip(), letzte[9].strip(), letzte[10].strip().lower()

        hinweise = []
        if warn and warn.lower() != "keine":
            hinweise.append({"art": "warnung", "text": warn})
        if info and info.lower() != "keine":
            hinweise.append({"art": "zustand", "text": info})

        ampel = "gruen" if farbe.startswith(("gruen", "grün")) else \
                "gelb" if farbe.startswith("gelb") else \
                "rot" if farbe.startswith("rot") else "unbekannt"
        lat, lon = BE_KOORDINATEN.get(name, (None, None))

        out.append(stelle(
            "BE", _be_finde_id(name, kandidaten), name,
            lat=lat, lon=lon,
            vertrauen="berechnet" if (ecoli is not None and entero is not None) else "jahresnote",
            ampel=ampel,
            probe=probedatum(letzte[1]),
            ecoli=ecoli, entero=entero,
            chlorophyll=zahl(letzte[6]),
            sichttiefe=zahl(letzte[5]),
            temperatur=zahl(letzte[7]),
            hinweise=hinweise,
            url=BE_SEITE,
        ))
    return out


BB_KML_URL = "https://badestellen.brandenburg.de/web/badestellen/badestellen/-/export/badestellen.kml"
BB_SEITE = "https://badestellen.brandenburg.de/"
_bb_detail = "https://badestellen.brandenburg.de/badestelle/-/details/{}".format

# Textbausteine aus dem Feld "remarks", die auf ein echtes Baderisiko
# hindeuten statt auf Ausstattung/Auszeichnung ("Blaue Flagge",
# "Barrierefreies Bad" o.ä.) - siehe Docstring unten.
BB_RISIKO_WOERTER = re.compile(
    r"nicht baden|badeverbot|blaualgen|algen|dermatitis|sichttiefe|"
    r"geogen|verschmutzung|trüb", re.I)


def konnektor_bb() -> list[dict]:
    """
    Brandenburg, 253 Badestellen. KML mit <ExtendedData><Data name="…"> je
    Placemark — deutlich einfacher zu parsen als die alte HTML-Beschreibung
    im <description>-Feld (die bleibt hier ungenutzt).

    Felder: lastMeasurementDate, temperature, visibilityDepth, remarks,
    bodyOfWater, name (nur der Ortsteil, z.B. "Bralitz" - der volle Name
    steht im <name>-Tag als "Ort, Gewässer"), bacteriology (Text, KEINE
    Zahlen: "keine Beanstandungen" / "mikrobiologisch zu beanstanden" / "-"),
    bnr (amtliche Nummer, siehe unten).

    bnr → ID: bnr="4" + Land "BB" ergibt DEBB_PR_0004, geprüft gegen die
    EEA-Basis (bnr 4 = "Joachimsthal, Feriendorf" = DEBB_PR_0004
    "GRIMNITZSEE JOACHIMSTHAL FERIENDORF").

    "smiley"/styleUrl (evaluation1-5, icons level0-5.png) NICHT gemappt:
    94 % aller Stellen sind evaluation1, aber auch etliche evaluation2/3-
    Stellen haben bacteriology="keine Beanstandungen" - die Skala korreliert
    nicht sauber mit Keimbelastung, sondern mit "rating" (vermutlich eine
    allgemeine Zustands-/Ausstattungsnote). Ohne offizielle Legende wäre das
    Raten an der einzigen Stelle, die am wichtigsten ist (ampel). EEA liefert
    die geprüfte Jahreseinstufung, die reicht hier.

    remarks ist echter Klartext und wird gefiltert: Treffer auf
    BB_RISIKO_WOERTER → hinweise als "warnung" (bestätigt am Beispiel "Alt
    Zeschdorf, Hohenjesarscher See": remarks "Empfehlung - nicht baden!" bei
    smiley=evaluation3, einziger Fund dieser Art). Alles andere (Blaue
    Flagge, Barrierefreies Bad, Steilstrand, …) als "zustand" - das sind
    Ausstattungs-/Zugangshinweise, keine Gesundheitswarnung.

    Keine Keimzahlen in dieser Quelle (bacteriology ist Text, keine KBE-
    Werte) → vertrauen bleibt "amtlich", nie "berechnet".
    """
    text = hole(BB_KML_URL).content.decode("utf-8")
    if "<Placemark" not in text:
        raise ValueError("Brandenburg: kein Placemark im KML")

    def feld(pm: str, name: str) -> str:
        m = re.search(rf'<Data name="{name}">\s*<value>([\s\S]*?)</value>', pm)
        if not m:
            return ""
        wert = html.unescape(m.group(1))
        wert = re.sub(r"<[^>]*>", " ", wert)
        wert = re.sub(r"\s+", " ", wert).strip()
        return "" if wert == "-" else wert

    def zahl(v: str):
        m = re.match(r"^([\d.,]+)", (v or "").strip())
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None

    def probedatum(v: str) -> str | None:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v or "")
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    out = []
    for pm in re.findall(r"<Placemark\b[\s\S]*?</Placemark>", text):
        bnr = feld(pm, "bnr")
        if not bnr or not bnr.isdigit():
            continue

        c = re.search(r"<coordinates>([^<]*)</coordinates>", pm)
        teile = (c.group(1).split(",") if c else [])
        lon = float(teile[0]) if len(teile) > 0 and teile[0].strip() else None
        lat = float(teile[1]) if len(teile) > 1 and teile[1].strip() else None

        ort = feld(pm, "name")
        gewaesser = feld(pm, "bodyOfWater")
        name = f"{ort}, {gewaesser}" if ort and gewaesser else (gewaesser or ort)

        remarks = feld(pm, "remarks")
        bakteriologie = feld(pm, "bacteriology")
        hinweise = []
        if remarks:
            hinweise.append({
                "art": "warnung" if BB_RISIKO_WOERTER.search(remarks) else "zustand",
                "text": remarks,
            })
        if bakteriologie and "beanstand" in bakteriologie.lower() and "keine" not in bakteriologie.lower():
            hinweise.append({"art": "warnung", "text": bakteriologie})

        out.append(stelle(
            "BB", f"DEBB_PR_{int(bnr):04d}", name,
            gewaesser=gewaesser or None, ort=ort or None,
            lat=lat, lon=lon,
            vertrauen="amtlich",
            probe=probedatum(feld(pm, "lastMeasurementDate")),
            temperatur=zahl(feld(pm, "temperature")),
            sichttiefe=zahl(feld(pm, "visibilityDepth")),
            hinweise=hinweise,
            url=_bb_detail(bnr),
        ))

    if len(out) < 20:
        raise ValueError(f"Brandenburg: nur {len(out)} Badestellen gelesen")
    return out


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
