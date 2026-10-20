#!/usr/bin/env python3
"""
Veille Impulse — scanner des marchés publics vidéo / photo / motion design.

Sources :
  - BOAMP (marchés publics français, y compris petits montants)  — API OpenDataSoft DILA
  - TED   (marchés européens au-dessus des seuils, filtrés France) — API officielle v3

Sortie :
  data/annonces.json   toutes les annonces ouvertes connues (historique, sans doublons)
  data/nouvelles.json  uniquement les annonces découvertes lors de ce passage
  data/run.json        résumé du passage (compteurs, erreurs)

Aucune dépendance externe : bibliothèque standard Python uniquement.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))

UA = "veille-impulse/1.0 (+https://impulsecreativeagency.com)"
TODAY = date.today()


# --------------------------------------------------------------------------- utils
def norm(text: str) -> str:
    """minuscules sans accents, pour comparer des mots-clés."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def http_json(url: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_date(value) -> str | None:
    """Renvoie AAAA-MM-JJ ou None."""
    if not value:
        return None
    if isinstance(value, list):
        dates = [parse_date(v) for v in value]
        dates = [d for d in dates if d]
        return min(dates) if dates else None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if m:
        return "-".join(m.groups())
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(value))
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


# --------------------------------------------------------------------------- scoring
POSITIFS = [(norm(k), w) for k, w in CONFIG["mots_cles_positifs"].items()]
NEGATIFS = [norm(k) for k in CONFIG["mots_cles_exclus"]]
DEPTS_PRIORITE = set(CONFIG["departements_prioritaires"])
DEPTS_REGION = set(CONFIG["departements_region"])
NUTS_PRIORITE = tuple(CONFIG["nuts_prioritaires"])
NUTS_REGION = tuple(CONFIG["nuts_region"])
CPV_CIBLES = tuple(CONFIG["cpv_cibles"])


def score_annonce(a: dict) -> dict:
    """Score de 0 à 100 + raisons lisibles. Les exclusions mettent le score à 0."""
    texte = norm(" ".join([a.get("titre", ""), a.get("description", ""), " ".join(a.get("descripteurs", []))]))
    raisons, score = [], 0

    for neg in NEGATIFS:
        if neg in texte:
            return {**a, "score": 0, "raisons": [f"exclu : « {neg} »"], "exclu": True}

    pts_mots = 0
    for mot, poids in POSITIFS:
        if mot in texte:
            pts_mots += poids
            raisons.append(mot)
    score += min(pts_mots, 45)

    cpvs = a.get("cpv", [])
    if any(str(c).startswith(CPV_CIBLES) for c in cpvs):
        score += 20
        raisons.append("CPV audiovisuel/photo")

    lieu = a.get("lieu", [])
    if any(d in DEPTS_PRIORITE for d in lieu) or any(str(n).startswith(NUTS_PRIORITE) for n in lieu):
        score += 25
        raisons.append("Rhône / Lyon")
    elif any(d in DEPTS_REGION for d in lieu) or any(str(n).startswith(NUTS_REGION) for n in lieu):
        score += 15
        raisons.append("Auvergne-Rhône-Alpes")
    elif not lieu:
        score += 5

    if a.get("date_limite"):
        jours = (date.fromisoformat(a["date_limite"]) - TODAY).days
        if jours >= 7:
            score += 10
        elif jours >= 3:
            score += 5
            raisons.append(f"délai court ({jours} j)")
        else:
            raisons.append(f"délai très court ({jours} j)")
    else:
        score += 5

    # sans aucun signal métier, l'annonce n'est pas pour nous
    if pts_mots == 0 and "CPV audiovisuel/photo" not in raisons:
        return {**a, "score": 0, "raisons": ["aucun signal vidéo/photo"], "exclu": True}

    return {**a, "score": min(score, 100), "raisons": raisons, "exclu": False}


# --------------------------------------------------------------------------- BOAMP
BOAMP_URL = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"


def fetch_boamp(depuis: date) -> list[dict]:
    out: dict[str, dict] = {}
    for kw in CONFIG["requetes_boamp"]:
        where = f'dateparution >= date\'{depuis.isoformat()}\' AND search(objet, "{kw}")'
        offset = 0
        while True:
            params = {"where": where, "limit": 100, "offset": offset, "order_by": "dateparution desc"}
            url = BOAMP_URL + "?" + urllib.parse.urlencode(params)
            res = http_json(url)
            rows = res.get("results", [])
            for r in rows:
                idweb = str(r.get("idweb") or r.get("id") or "")
                if not idweb or idweb in out:
                    continue
                nature = norm(str(r.get("nature_libelle") or r.get("nature") or ""))
                if "attribution" in nature or "resultat" in nature:
                    continue  # avis de résultat : le marché est déjà attribué
                depts = r.get("code_departement") or []
                if isinstance(depts, str):
                    depts = [depts]
                desc = r.get("descripteur_libelle") or []
                if isinstance(desc, str):
                    desc = [desc]
                out[idweb] = {
                    "id": f"boamp-{idweb}",
                    "source": "BOAMP",
                    "titre": (r.get("objet") or "").strip(),
                    "acheteur": (r.get("nomacheteur") or "").strip(),
                    "lieu": [str(d).zfill(2) if str(d).isdigit() else str(d) for d in depts],
                    "date_publication": parse_date(r.get("dateparution")),
                    "date_limite": parse_date(r.get("datelimitereponse")),
                    "type": r.get("type_marche") or r.get("type_marche_facette") or "",
                    "procedure": r.get("procedure_libelle") or "",
                    "descripteurs": [str(x) for x in desc],
                    "cpv": [],
                    "description": "",
                    "url": r.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:%22{idweb}%22",
                }
            if len(rows) < 100 or offset >= 900:
                break
            offset += 100
    return list(out.values())


# --------------------------------------------------------------------------- TED
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_FIELDS = [
    "publication-number", "notice-title", "buyer-name", "publication-date",
    "deadline-receipt-tender-date-lot", "place-of-performance", "classification-cpv",
    "notice-type", "description-lot", "estimated-value-lot",
]


def pick_lang(value) -> str:
    if isinstance(value, dict):
        for lang in ("fra", "FRA", "eng", "ENG"):
            if value.get(lang):
                v = value[lang]
                return v[0] if isinstance(v, list) else v
        first = next(iter(value.values()), "")
        return first[0] if isinstance(first, list) else first
    if isinstance(value, list):
        return pick_lang(value[0]) if value else ""
    return value or ""


def fetch_ted(depuis: date) -> list[dict]:
    cpv = " ".join(CONFIG["cpv_ted"])
    query = (f"classification-cpv IN ({cpv}) AND buyer-country=FRA "
             f"AND publication-date>={depuis.strftime('%Y%m%d')}")
    out, page = [], 1
    while True:
        res = http_json(TED_URL, {"query": query, "fields": TED_FIELDS, "limit": 100, "page": page})
        notices = res.get("notices", [])
        for n in notices:
            ntype = str(n.get("notice-type") or "")
            if ntype.startswith(("can", "veat", "corr")):  # attributions, rectificatifs
                continue
            num = n.get("publication-number")
            out.append({
                "id": f"ted-{num}",
                "source": "TED",
                "titre": pick_lang(n.get("notice-title")).strip(),
                "acheteur": pick_lang(n.get("buyer-name")).strip(),
                "lieu": [str(x) for x in (n.get("place-of-performance") or []) if x not in ("anyw", "FRA")],
                "date_publication": parse_date(n.get("publication-date")),
                "date_limite": parse_date(n.get("deadline-receipt-tender-date-lot")),
                "type": "Services",
                "procedure": ntype,
                "descripteurs": [],
                "cpv": [str(c) for c in (n.get("classification-cpv") or [])],
                "description": pick_lang(n.get("description-lot"))[:600],
                "url": f"https://ted.europa.eu/fr/notice/-/detail/{num}",
            })
        if len(notices) < 100 or page >= 5:
            break
        page += 1
    return out


# --------------------------------------------------------------------------- main
def main() -> int:
    DATA.mkdir(exist_ok=True)
    fichier = DATA / "annonces.json"
    connues: dict[str, dict] = {}
    if fichier.exists():
        connues = {a["id"]: a for a in json.loads(fichier.read_text(encoding="utf-8"))}

    jours = int(os.environ.get("VEILLE_JOURS", CONFIG["fenetre_jours"]))
    depuis = TODAY - timedelta(days=jours)
    run = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "depuis": depuis.isoformat(),
           "sources": {}, "erreurs": []}

    brutes: list[dict] = []
    for nom, fn in (("BOAMP", fetch_boamp), ("TED", fetch_ted)):
        try:
            res = fn(depuis)
            run["sources"][nom] = len(res)
            brutes.extend(res)
        except Exception as e:  # une source en panne ne doit pas bloquer l'autre
            run["sources"][nom] = None
            run["erreurs"].append(f"{nom}: {type(e).__name__}: {e}")
            print(f"[ERREUR] {nom}: {e}", file=sys.stderr)

    nouvelles = []
    for a in brutes:
        a = score_annonce(a)
        if a["exclu"] or a["score"] < CONFIG["score_minimum"]:
            connues.pop(a["id"], None)  # un réglage plus strict retire aussi les anciennes
            continue
        if a.get("date_limite") and a["date_limite"] < TODAY.isoformat():
            continue
        if a["id"] in connues:
            a["vu_le"] = connues[a["id"]].get("vu_le")
            connues[a["id"]] = a
        else:
            a["vu_le"] = TODAY.isoformat()
            connues[a["id"]] = a
            nouvelles.append(a)

    # on oublie les annonces clôturées depuis plus de 30 jours
    limite = (TODAY - timedelta(days=30)).isoformat()
    connues = {k: v for k, v in connues.items() if not v.get("date_limite") or v["date_limite"] >= limite}

    trier = lambda xs: sorted(xs, key=lambda a: (-a["score"], a.get("date_limite") or "9999"))
    fichier.write_text(json.dumps(trier(connues.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "nouvelles.json").write_text(json.dumps(trier(nouvelles), ensure_ascii=False, indent=1), encoding="utf-8")
    run["nouvelles"] = len(nouvelles)
    run["total_suivies"] = len(connues)
    (DATA / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{len(brutes)} avis lus · {len(nouvelles)} nouvelles · {len(connues)} suivies · erreurs: {run['erreurs'] or 'aucune'}")
    return 1 if len(run["erreurs"]) == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
