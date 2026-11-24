#!/usr/bin/env python3
"""
enquetes_vs.py — Mises à l'enquête publique près de chez toi (Suisse romande).

VS et BE (avis en français) : amtsblattportal.ch ; VD : CAMAC (ogc.vd.ch) ; GE : SITG ;
FR : fr.ch (moteur de recherche) ; NE : SITN (WFS) ; JU : Journal officiel (PDF, pdftotext).
Page générée en français, allemand et italien.

Source : Bulletin officiel du Valais, publié via l'API publique du
Portail des feuilles officielles (amtsblattportal.ch, SECO), rubriques
BA-VS05 (demandes d'autorisation de construire) et BA-VS10 (mises en
consultation publique des plans).
Géocodage des communes / NPA : api3.geo.admin.ch (swisstopo).

Aucune dépendance : Python 3.8+ standard.

Exemples :
  python3 enquetes_vs.py                          # Evionnaz, 2 km, 30 jours
  python3 enquetes_vs.py --home Martigny --rayon 8
  python3 enquetes_vs.py --jours 60 --tous        # y c. délais échus
  python3 enquetes_vs.py --json enquetes.json     # export brut en plus
  python3 enquetes_vs.py --debug                  # dump XML du 1er avis

La page générée contient tous les avis du canton : le rayon (1 à 10 km) et la
commune de référence se règlent ensuite directement dans la page.

Coordonnées des communes : data/communes_vs.json (scripts/maj_communes.py),
corrigées par data/communes_vs_manuel.json, complétées par les avis eux-mêmes.
"""
import argparse, datetime as dt, html, json, math, os, re, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

API = "https://amtsblattportal.ch/api/v1"
GEO = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
DETAIL_URL = "https://www.bulletin-officiel.vs.ch/#!/search/publications/detail/{id}"
ECONSTRUCTION_URL = "https://econstruction-vs.apps.vs.ch/"  # consultation : connexion SwissID, puis recherche par numéro
SUBRUBRIQUES = {"BA-VS05": "Autorisation de construire", "BA-VS10": "Consultation des plans"}
DELAI_OPPOSITION_J = 30  # LC Valais : 30 jours dès la publication au BO (si l'avis n'indique pas de date)
CACHE = os.path.expanduser("~/.cache/enquetes_vs_geo.json")
UA = {"User-Agent": "enquetes-vs/1.1 (usage personnel)", "Accept": "application/json"}


# ---------- HTTP ----------
def get(url, params=None, accept="application/json", retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={**UA, "Accept": accept})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            if i == retries - 1:
                raise RuntimeError(f"{url} → {e}")
            time.sleep(1.5 * (i + 1))


def get_json(url, params=None):
    return json.loads(get(url, params).decode("utf-8"))


# ---------- Géocodage ----------
_geo = {}
def _load_cache():
    global _geo
    try:
        with open(CACHE, encoding="utf-8") as f:
            _geo = json.load(f)
    except Exception:
        _geo = {}

def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(_geo, f, ensure_ascii=False)
    except OSError:
        pass

def _village(name):
    """Centre du lieu habité (« Ort » du gazetteer swisstopo) portant exactement ce nom."""
    try:
        d = get_json(GEO, {"searchText": name, "type": "locations",
                           "origins": "gazetteer", "limit": 15, "sr": 4326})
    except Exception:
        return None
    for r in d.get("results", []):
        a = r.get("attrs", {})
        lab = a.get("label", "")
        if "<i>Ort</i>" in lab and re.search(r"<b>\s*" + re.escape(name) + r"\s*</b>", lab, re.I) and "lat" in a:
            return a["lat"], a["lon"]
    return None


def geocode(text, origins="gg25,zipcode"):
    """Commune (gg25) ou NPA → (lat, lon, label), au centre du village si possible. None si introuvable.
    Le centre géométrique d'une commune peut tomber loin du village (montagne) : on lui préfère
    le lieu habité du même nom."""
    if not text:
        return None
    key = f"v2|{origins}|{text.strip().lower()}"
    if key in _geo:
        return tuple(_geo[key]) if _geo[key] else None
    res = None
    try:
        d = get_json(GEO, {"searchText": text, "type": "locations",
                           "origins": origins, "limit": 3, "sr": 4326})
        for r in d.get("results", []):
            a = r.get("attrs", {})
            if "lat" in a and "lon" in a:
                label = re.sub(r"<[^>]+>", "", a.get("label", text)).strip()
                name = re.sub(r"^\d{4}\s*-\s*|\s*\([A-Z]{2}\)$", "", label).strip()
                v = _village(name)
                res = (v[0], v[1], label) if v else (a["lat"], a["lon"], label)
                break
    except Exception:
        res = None
    _geo[key] = res
    return res

def km(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def lv95_to_wgs84(text):
    """« 2'585'998 1'117'677 » (LV95, ou LV03) → (lat, lon). Formules approchées swisstopo (~1 m)."""
    nums = [float(n.replace("'", "").replace("’", "")) for n in re.findall(r"\d[\d'’]*(?:\.\d+)?", text or "")]
    if len(nums) < 2:
        return None
    e, n = nums[0], nums[1]
    if e < n:                      # ordre inversé
        e, n = n, e
    if e < 1_000_000:              # LV03 → LV95
        e, n = e + 2_000_000, n + 1_000_000
    if not (2_480_000 < e < 2_840_000 and 1_070_000 < n < 1_300_000):
        return None
    y, x = (e - 2_600_000) / 1e6, (n - 1_200_000) / 1e6
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y ** 3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x ** 3
    return (lat * 100 / 36, lon * 100 / 36)


# ---------- Publications ----------
def list_publications(subrubrics, start, end, page_size=1000, max_pages=20):
    # Grandes pages : la pagination de l'API n'est pas stable (doublons et avis manquants avec des pages de 100).
    out, page = [], 0
    while page < max_pages:
        d = get_json(f"{API}/publications", {
            "publicationStates": "PUBLISHED",
            "subRubrics": subrubrics,
            "publicationDate.start": start.isoformat(),
            "publicationDate.end": end.isoformat(),
            "pageRequest.page": page,
            "pageRequest.size": page_size,
        })
        items = d.get("content") or []
        out.extend(items)
        total = d.get("total", len(out))
        if total > 20000:  # garde-fou : filtre ignoré par l'API
            raise RuntimeError("L'API a ignoré les filtres (résultat anormal). Lance avec --debug.")
        if len(items) < page_size or len(out) >= total:
            break
        page += 1
    return out


def _t(e, path):
    """Texte d'un sous-élément ; pour un « term », préfère le français."""
    if e is None:
        return ""
    node = e.find(path)
    if node is None:
        return ""
    if len(node):
        for lang in ("fr", "de", "it", "en"):
            v = node.findtext(lang)
            if v and v.strip():
                return v.strip()
        return ""
    return (node.text or "").strip()


def _strip_ns(root):
    for e in root.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return root


def element_value(el):
    """Valeur lisible d'un <element> de businessCase, selon son valueType."""
    vt = el.findtext("valueType") or ""
    if vt == "text":
        return _t(el, "valueText/term")
    if vt == "textNeutral":
        return (el.findtext("valueTextNeutral") or "").strip()
    if vt == "naturalLegalPerson":
        s = el.find("subject")
        if s is None:
            return ""
        name = _t(s, "legalPerson/company/name")
        if not name:
            np_ = s.find("naturalPerson")
            if np_ is not None:
                name = " ".join(x for x in (np_.findtext("firstName"), np_.findtext("officialName")) if x)
        return name.strip()
    # autre type : premier texte trouvé
    for sub in el.iter():
        if sub is not el and sub.tag not in ("key", "valueType", "term") and (sub.text or "").strip() and not len(sub):
            return sub.text.strip()
    return ""


def clean_contact(text):
    """Adresse de l'autorité (texte simple ou HTML) → lignes de texte."""
    t = re.sub(r"(?i)<br\s*/?>|</(div|p)>", "\n", text or "")
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    lines = [re.sub(r"\s+", " ", l).strip(" ,") for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def parse_detail(pid, raw_xml):
    root = _strip_ns(ET.fromstring(raw_xml))
    meta = root.find("meta")
    content = root.find("content")
    bc = content.find("businessCase") if content is not None else None

    fields = {}
    if bc is not None:
        for el in bc.iter("element"):
            k = (el.findtext("key") or "").strip()
            v = element_value(el)
            if not v:
                continue
            if not k:
                k = "_" + _t(el, "term")
            fields.setdefault(k.lower(), [])
            if v not in fields[k.lower()]:
                fields[k.lower()].append(v)
    projet = ""
    if bc is not None:
        prim = bc.find("primary/element")
        if prim is not None:
            projet = element_value(prim)
    projet = projet or ", ".join(fields.get("constructionprojecttitle", []))

    titre = _t(meta, "title")
    sub = meta.findtext("subRubric") or ""
    if not projet and titre:
        projet = re.sub(r"^[^–-]+[–-]\s*", "", titre)

    reaction = content.find("reaction/element") if content is not None else None
    deadline = (reaction.findtext("deadline/date") if reaction is not None else "") or ""
    deadline_label = _t(reaction, "deadline/term") if reaction is not None else ""
    contact = clean_contact(_t(reaction, "contact")) if reaction is not None else ""

    commune = _t(content, "localization/municipalityId/term") if content is not None else ""
    office = meta.findtext("registrationOffice/displayName") or ""
    coords = lv95_to_wgs84(", ".join(fields.get("coordinates", [])))

    g = lambda k: ", ".join(fields.get(k, []))
    return {
        "id": pid,
        "numero": meta.findtext("publicationNumber") or "",
        "langue": (meta.findtext("language") or "fr")[:2],
        "type": SUBRUBRIQUES.get(sub, sub),
        "titre": titre,
        "projet": projet,
        "date": (meta.findtext("publicationDate") or "")[:10],
        "delai": deadline[:10],
        "delai_libelle": deadline_label,
        "commune": commune or (meta.findtext("registrationOffice/town") or ""),
        "office": office,
        "npa": meta.findtext("registrationOffice/swissZipCode") or "",
        "parcelle": g("parcel"),
        "zone": g("zone"),
        "derogations": g("exeption") or g("exception"),
        "requerant": g("applicantparty"),
        "proprietaire": g("landowner"),
        "auteur": g("projectframer"),
        "econstruction": g("caseno"),
        "bfs": (content.findtext("localization/municipalityId/key") if content is not None else "") or "",
        "ro_bfs": meta.findtext("registrationOffice/municipalityId") or "",
        "ro_nom": office,
        "ro_adresse": ", ".join(x for x in (
            " ".join(y for y in (meta.findtext("registrationOffice/street"), meta.findtext("registrationOffice/streetNumber")) if y),
            " ".join(y for y in (meta.findtext("registrationOffice/swissZipCode"), meta.findtext("registrationOffice/town")) if y)) if x),
        "contact": contact,
        "coords_exactes": bool(coords),
        "lat": coords[0] if coords else None,
        "lon": coords[1] if coords else None,
        "url": DETAIL_URL.format(id=pid),
    }


def fetch_one(pid):
    raw = get(f"{API}/publications/{pid}/xml", accept="application/xml")
    return raw, parse_detail(pid, raw)


# ---------- HTML ----------
TEMPLATE = r"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mises à l’enquête – Suisse romande</title>
<meta name="description" content="Les mises à l'enquête publique (constructions et plans) en Suisse romande, près de chez vous. Öffentliche Auflagen von Baugesuchen. Pubblicazioni edilizie.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23d52b1e'/><path d='M13 7h6v6h6v6h-6v6h-6v-6H7v-6h6z' fill='white'/></svg>">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#fff;--ink:#14181d;--mut:#5b6470;--red:#d52b1e;--line:#e3e6ea;--soft:#f5f6f8;--hi:#fff4f3;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--ink:#e8ecf0;--mut:#9aa4ae;--red:#ff5a4c;--line:#2a3038;--soft:#181c21;--hi:#2a1615;color-scheme:dark}}
*{box-sizing:border-box;min-width:0}body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:24px max(4vw,16px) 16px;border-top:6px solid var(--red);position:relative}
.lng{position:absolute;top:14px;right:max(4vw,16px);display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.lng button{font:600 13px system-ui;border:0;background:var(--bg);color:var(--mut);padding:6px 10px;cursor:pointer}
.lng button[aria-pressed=true]{background:var(--red);color:#fff}
@media (max-width:560px){h1{padding-right:120px}}
h1{margin:0 0 4px;font-size:clamp(26px,4vw,40px);line-height:1.1;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:15px}
.ctl{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:end;margin-top:16px}
.ctl label,.fld{display:grid;gap:4px;font-size:13px;color:var(--mut);font-weight:600}
.acw{position:relative;display:flex;gap:6px}
#gps{flex:none;width:42px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);cursor:pointer;display:grid;place-items:center;padding:0}
#gps:hover{border-color:var(--mut)}#gps[aria-busy=true] svg{animation:spin 1s linear infinite}#gps.on{color:var(--red);border-color:var(--red)}
@keyframes spin{to{transform:rotate(360deg)}}
#hl{position:absolute;z-index:1000;top:calc(100% + 4px);left:0;right:48px;min-width:220px;margin:0;padding:4px;list-style:none;background:var(--bg);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.15);max-height:320px;overflow:auto}
#hl li{padding:8px 10px;border-radius:6px;cursor:pointer;color:var(--ink);font-weight:500;font-size:15px;display:flex;justify-content:space-between;gap:12px}
#hl li small{color:var(--mut);font-weight:400;white-space:nowrap}
#hl li[aria-selected=true],#hl li:hover{background:var(--soft)}
#hl mark{background:none;color:var(--red);font-weight:700}
input,select{font:16px system-ui;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
#home{width:240px}#q{width:min(340px,100%)}
input[type=range]{padding:0;accent-color:var(--red);width:180px}
.chk{display:flex!important;align-items:center;gap:8px;font-weight:500!important;color:var(--ink)!important;font-size:15px!important;padding-bottom:9px}
.chk input{accent-color:var(--red)}
#err{color:var(--red);font-size:14px}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);border-top:1px solid var(--line)}
#map{height:calc(100vh - 0px);position:sticky;top:0}
main{padding:8px max(3vw,16px) 40px}
@media (max-width:900px){.layout{grid-template-columns:1fr}#map{height:52vh;position:relative}}
.c{border-bottom:1px solid var(--line);padding:16px 12px;margin:0 -12px;border-radius:8px;cursor:pointer}
.c:hover{background:var(--soft)}.c.on{background:var(--hi)}
.h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.d{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}.com{color:var(--mut)}
.tp{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:1px 8px}.tp.kt{font-weight:700;color:var(--ink)}
.b{margin-left:auto;font-size:13px;padding:2px 9px;border-radius:99px;background:var(--line);white-space:nowrap;font-weight:600}
.b.hot{background:var(--red);color:#fff}.b.late{opacity:.55}
h3{margin:6px 0 4px;font-size:17px;line-height:1.35}
a{color:inherit}h3 a{text-decoration:none}h3 a:hover{text-decoration:underline}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:6px 0;font-size:14px}
dt{color:var(--mut)}dd{margin:0}
small{color:var(--mut)}.foot{color:var(--mut);font-size:13px;margin-top:24px}
.empty{color:var(--mut);padding:24px 0}
.ec{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap}
.ec code{font:600 14px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.ecb{font:600 13px system-ui;color:var(--red);background:none;border:1px solid currentColor;border-radius:99px;padding:3px 10px;cursor:pointer;white-space:nowrap}
.ecb:hover{background:var(--red);color:#fff}
#toast{position:fixed;left:0;right:0;bottom:20px;margin:0 auto;width:fit-content;z-index:2000;max-width:min(520px,calc(100vw - 32px));background:var(--ink);color:var(--bg);padding:12px 16px;border-radius:10px;font-size:14px;line-height:1.4;box-shadow:0 8px 24px rgba(0,0,0,.25)}
#toast b{font-family:ui-monospace,Menlo,monospace}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start;margin-top:8px}
.oppb{font:600 13px system-ui;color:#fff;background:var(--red);border:1px solid var(--red);border-radius:99px;padding:4px 12px;cursor:pointer}
.oppb:hover{filter:brightness(1.1)}
#opp{width:min(680px,calc(100vw - 16px));max-height:calc(100vh - 24px);padding:0;border:1px solid var(--line);border-radius:14px;background:var(--bg);color:var(--ink);box-shadow:0 20px 60px rgba(0,0,0,.35)}
#opp::backdrop{background:rgba(10,12,16,.55)}
#opp form{display:grid;gap:14px;padding:20px}
#opp .hd{display:flex;justify-content:space-between;align-items:start;gap:12px}
#opp h2{margin:0;font-size:22px}
#opp .x{border:0;background:none;color:var(--mut);font-size:26px;line-height:1;cursor:pointer;padding:0 4px}
.ref{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:14px}
.ref b{display:block;font-size:15px;margin-bottom:2px}
.dl{border-left:4px solid var(--red);padding:8px 12px;background:var(--hi);border-radius:0 8px 8px 0;font-size:14px;margin:0}
#opp fieldset{border:0;padding:0;margin:0;display:flex;gap:8px;flex-wrap:wrap}
#opp legend{font-size:13px;color:var(--mut);font-weight:600;margin-bottom:6px;padding:0}
.rad{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:8px;padding:8px 12px;cursor:pointer;font-size:15px}
.rad:has(input:checked){border-color:var(--red);background:var(--hi)}.rad input{accent-color:var(--red)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px}
@media (max-width:560px){.g2{grid-template-columns:1fr}}
#opp label.f{display:grid;gap:4px;font-size:13px;color:var(--mut);font-weight:600}
#opp label.f i{font-style:normal;font-weight:400}
#opp input:not([type=radio]),#opp select,#opp textarea{width:100%}
#opp textarea{font:15px/1.5 system-ui;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);min-height:150px;resize:vertical}
.how{font-size:14px;margin:0;padding-left:20px}.how li{margin:3px 0}
.fine{font-size:12.5px;color:var(--mut);margin:0}
.fb{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.btn{font:600 15px system-ui;border-radius:8px;padding:10px 16px;cursor:pointer;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.btn.p{background:var(--red);border-color:var(--red);color:#fff}
.ci{font-size:14px}.ci summary{cursor:pointer;font:600 13px system-ui;color:var(--ink);border:1px solid var(--line);border-radius:99px;padding:4px 12px;list-style:none;display:inline-block}
.ci summary::-webkit-details-marker{display:none}.ci[open] summary{background:var(--soft)}
.ci .box{margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--soft);display:grid;gap:3px}
.ci .box b{font-size:14px}.ci .box a{color:var(--red)}.ci .muted{color:var(--mut);font-size:12.5px}
#letter{display:none}
@media print{@page{margin:20mm 20mm 18mm}body{background:#fff!important;color:#000!important}body>*:not(#letter){display:none!important}
 #letter{display:block;font:11pt/1.5 Georgia,"Times New Roman",serif;color:#000}
 #letter .from{margin-bottom:14mm}#letter .to{margin-left:90mm;margin-bottom:12mm;white-space:pre-line}
 #letter .rec{font-weight:bold;margin-bottom:2mm}#letter .date{text-align:right;margin-bottom:8mm}
 #letter .obj{font-weight:bold;margin:0 0 2mm}#letter .refs{margin:0 0 6mm;font-size:10pt}
 #letter p{margin:0 0 4mm}#letter .mot{white-space:pre-wrap;margin:0 0 4mm}
 #letter .sig{margin-top:10mm;margin-left:90mm}#letter .line{margin-top:18mm;border-top:1px solid #000;padding-top:1mm;font-size:9pt}}
@media (max-width:560px){.ctl{display:grid;grid-template-columns:1fr 1fr}.ctl .fld,.ctl label:nth-child(3),.ctl #err{grid-column:1/-1}#home,#q,input[type=range]{width:100%}#home{flex:1}}
</style></head><body>
<header>
 <div class="lng" role="group" aria-label="Langue / Sprache / Lingua"><button type="button" data-l="fr" lang="fr">FR</button><button type="button" data-l="de" lang="de">DE</button><button type="button" data-l="it" lang="it">IT</button></div>
 <h1 id="h1">Mises à l'enquête</h1>
 <div class="sub" id="sub"></div>
 <div class="ctl">
  <div class="fld"><label for="home" data-i18n="home">Autour de</label><div class="acw"><input id="home" type="text" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="hl" autocomplete="off" spellcheck="false" placeholder="Commune ou NPA" data-i18n-ph="ph_home"><button id="gps" type="button" title="Utiliser ma position" aria-label="Utiliser ma position" data-i18n-title="gps"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5" fill="currentColor"/><path d="M12 1v4M12 19v4M1 12h4M19 12h4"/></svg></button><ul id="hl" role="listbox" aria-label="Communes" hidden></ul></div></div>
  <label><span id="rl"></span><input type="range" id="r" min="1" max="10" step="1"></label>
  <label><span data-i18n="search">Rechercher</span><input id="q" type="search" placeholder="Commune, mot-clé, parcelle, requérant…" data-i18n-ph="ph_search"></label>
  <label><span data-i18n="canton">Canton</span><select id="k"><option value="" data-i18n="all">Tous</option></select></label>
  <label><span data-i18n="type">Type</span><select id="t"><option value="" data-i18n="all">Tous</option></select></label>
  <label class="chk"><input type="checkbox" id="late"> <span data-i18n="late">Inclure les délais échus</span></label>
  <span id="err" role="status"></span>
 </div>
</header>
<div id="toast" role="status" aria-live="polite" hidden></div>
<dialog id="opp" aria-labelledby="opp-t">
 <form id="opp-f" novalidate>
  <div class="hd"><h2 id="opp-t">Faire opposition</h2><button type="button" class="x" id="opp-x" aria-label="Fermer" data-i18n-title="close">×</button></div>
  <div class="ref" id="opp-ref"></div>
  <p class="dl" id="opp-dl"></p>
  <fieldset><legend data-i18n="d_wish">Je souhaite</legend>
   <label class="rad"><input type="radio" name="kind" value="opposition" checked> <span data-i18n="d_opp">former une opposition</span></label>
   <label class="rad"><input type="radio" name="kind" value="reserve"> <span data-i18n="d_res">formuler une réserve de droit privé</span></label>
  </fieldset>
  <div class="g2">
   <label class="f"><span data-i18n="f_prenom">Prénom</span><input name="prenom" required autocomplete="given-name"></label>
   <label class="f"><span data-i18n="f_nom">Nom</span><input name="nom" required autocomplete="family-name"></label>
   <label class="f"><span data-i18n="f_adr">Adresse</span><input name="adresse" required autocomplete="street-address"></label>
   <label class="f"><span data-i18n="f_loc">NPA et localité</span><input name="localite" required autocomplete="postal-code address-level2" placeholder="1902 Evionnaz"></label>
   <label class="f"><span><span data-i18n="f_mail">E-mail</span> <i data-i18n="opt">(facultatif)</i></span><input name="email" type="email" autocomplete="email"></label>
   <label class="f"><span><span data-i18n="f_tel">Téléphone</span> <i data-i18n="opt">(facultatif)</i></span><input name="tel" type="tel" autocomplete="tel"></label>
  </div>
  <div class="g2">
   <label class="f"><span data-i18n="f_qual">Qualité pour agir</span><select name="qualite" required>
    <option value="" data-i18n="choose">Choisir…</option>
    <option value="q1" data-i18n="q1">propriétaire d’une parcelle voisine</option>
    <option value="q2" data-i18n="q2">locataire ou habitant·e à proximité immédiate</option>
    <option value="q3" data-i18n="q3">propriétaire ou habitant·e touché·e par les nuisances du projet</option>
    <option value="q4" data-i18n="q4">autre</option></select></label>
   <label class="f"><span><span data-i18n="f_prec">Précision</span> <i data-i18n="f_prec2">(parcelle, distance…)</i></span><input name="precision" placeholder="ex. parcelle n° 1234, à 20 m du projet" data-i18n-ph="ph_prec"></label>
  </div>
  <label class="f"><span data-i18n="f_mot">Motifs</span><textarea name="motifs" required data-i18n-ph="ph_mot"></textarea></label>
  <ol class="how" id="opp-how"></ol>
  <p class="fine" data-i18n="fine"></p>
  <div class="fb"><button type="button" class="btn" id="opp-c" data-i18n="cancel">Annuler</button><button type="submit" class="btn p" data-i18n="gen">Générer la lettre (imprimer / PDF)</button></div>
 </form>
</dialog>
<article id="letter" aria-hidden="true"></article>
<div class="layout">
 <div id="map"></div>
 <main><section id="list"></section>
 <p class="foot" id="foot"></p></main>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
const DATA=__DATA__, CFG=__CFG__, COMMUNES=__COMMUNES__;
// ---------- Traductions de l'interface (FR / DE / IT) ----------
const I18N={
fr:{title:'Mises à l’enquête',home:'Autour de',ph_home:'Commune ou NPA',gps:'Utiliser ma position',radius:'Rayon : {0} km',search:'Rechercher',ph_search:'Commune, mot-clé, parcelle, requérant…',canton:'Canton',type:'Type',all:'Tous',late:'Inclure les délais échus',lang:'Langue',
 h1:(n,r)=>n+' mise'+(n>1?'s':'')+' à l’enquête à moins de '+r+' km',sub:'Autour de {0} · {1} avis ({2}) des {3} derniers jours',empty:'Aucune mise à l’enquête ne correspond dans ce rayon.',
 late_b:'échu',last:'dernier jour',left:'J-{0}',running:'en cours',nature:'Nature',camac:'N° CAMAC',dossier:'N° dossier',parcelle:'Parcelle',zone:'Zone',derog:'Dérogations',req:'Requérant',lieu:'Lieu',
 b_opp:'Faire opposition',b_obs:'Faire des observations',contact:'Contacter la commune',period:'du {0} au {1}',period_to:'jusqu’au {0}',
 l_inq:'Enquête publique {0} · oppositions jusqu’au {1}',l_ge:'Déposé le {0} · observations : 30 jours dès la publication dans la FAO',l_pub:'Publié le {0} · oppositions jusqu’au {1}',l_approx:' · position approximative (centre de la commune)',
 ec_b:'Copier et ouvrir eConstruction ↗',ec_t:'Copie le numéro {0} et ouvre eConstruction (connexion SwissID requise)',ec_ok:'Numéro {0} copié. Sur eConstruction, connectez-vous avec SwissID, puis « Consultation du Bulletin Officiel » et collez-le dans la recherche.',ec_ko:'Notez le numéro {0} : sur eConstruction, connectez-vous avec SwissID, puis « Consultation du Bulletin Officiel » et saisissez-le dans la recherche.',
 admin:n=>'Administration communale '+deFr(n),tel:'Tél. ',mail:'E-mail ',hours:'Horaires : {0}',site:'Site de la commune ↗',site_h:'Horaires et contact sur le site de la commune ↗',na:'Coordonnées non disponibles.',
 notice:'Avis officiel ↗',ref:'Point de référence',n_avis:'{0} avis',locality:n=>'localité '+deFr(n),notfound:'Commune introuvable.',unavail:'Recherche indisponible.',geo_na:'Géolocalisation non disponible sur cet appareil.',locating:'Localisation…',denied:'Accès à la position refusé : autorisez-le dans le navigateur.',pos_na:'Position indisponible pour le moment.',
 me:'Ma position',near:n=>(/^[aeiouyàâéèêëîïôûü]/i.test(n)?'près d’':'près de ')+n,
 d_wish:'Je souhaite',d_opp:'former une opposition',d_res:'formuler une réserve de droit privé',f_prenom:'Prénom',f_nom:'Nom',f_adr:'Adresse',f_loc:'NPA et localité',f_mail:'E-mail',opt:'(facultatif)',f_tel:'Téléphone',f_qual:'Qualité pour agir',choose:'Choisir…',
 q1:'propriétaire d’une parcelle voisine',q2:'locataire ou habitant·e à proximité immédiate',q3:'propriétaire ou habitant·e touché·e par les nuisances du projet',q4:'autre',
 f_prec:'Précision',f_prec2:'(parcelle, distance…)',ph_prec:'ex. parcelle n° 1234, à 20 m du projet',f_mot:'Motifs',ph_mot:'Expliquez en quoi le projet vous touche personnellement : hauteur, ombre, vue, bruit, circulation, non-conformité au règlement de zone, dérogations…',
 fine:'Rien n’est envoyé : la lettre est préparée dans votre navigateur. Vos coordonnées sont mémorisées sur cet appareil seulement. Modèle indicatif, qui ne remplace pas un conseil juridique.',cancel:'Annuler',gen:'Générer la lettre (imprimer / PDF)',close:'Fermer',
 r_inq:'Enquête publique {0}',r_dos:'Dossier {0}, déposé le {1}',r_pub:'Publié le {0}',
 dl_ge:'Délai : 30 jours dès la publication de la demande dans la Feuille d’avis officielle (FAO). Vérifiez la date de publication sur fao.ge.ch.',dl:'À envoyer au plus tard le {0} (timbre postal faisant foi)',dl_today:' : c’est aujourd’hui !',dl_left:n=>' : encore '+n+' jour'+(n>1?'s':'')+'.',
 h_consult:'Avant d’écrire, consultez le dossier auprès de l’administration communale ({0}){1}.',h_online:', ou en ligne (lien de l’avis)',h_ec:', ou en ligne sur eConstruction',h_print:'Générez la lettre, imprimez-la (ou enregistrez-la en PDF) et signez-la à la main.',h_send:'Envoyez-la de préférence en recommandé à : {0}.',h_ecdep:'Ou déposez-la en PDF sur eConstruction (n° {0}) : il faut alors une signature électronique qualifiée et une identité vérifiée. ',h_lang:'La lettre est rédigée en {0}, la langue de l’avis officiel.',
 lname:{fr:'français',de:'allemand'},
 foot:'Sources : VS Bulletin officiel du Valais ; BE Feuille officielle du canton de Berne (avis en français) ; VD CAMAC, géodonnées de l’État de Vaud ; GE SITG ; FR moteur de recherche de l’État de Fribourg ; NE SITN ; JU Journal officiel (PDF). Généré le {0} (avis des {1} derniers jours). Les liens ouvrent l’avis ou le dossier officiel : seule la publication officielle fait foi. GE : la date de publication dans la FAO n’est pas fournie ; le délai d’observations est de 30 jours dès cette publication.'},
de:{title:'Öffentliche Auflagen',home:'Rund um',ph_home:'Gemeinde oder PLZ',gps:'Meinen Standort verwenden',radius:'Umkreis: {0} km',search:'Suchen',ph_search:'Gemeinde, Stichwort, Parzelle, Gesuchsteller…',canton:'Kanton',type:'Art',all:'Alle',late:'Abgelaufene Fristen anzeigen',lang:'Sprache',
 h1:(n,r)=>n+' '+(n===1?'Auflage':'Auflagen')+' im Umkreis von '+r+' km',sub:'Rund um {0} · {1} Publikationen ({2}) der letzten {3} Tage',empty:'Keine Auflage in diesem Umkreis.',
 late_b:'abgelaufen',last:'letzter Tag',left:'noch {0} T.',running:'laufend',nature:'Art der Arbeiten',camac:'CAMAC-Nr.',dossier:'Dossier-Nr.',parcelle:'Parzelle',zone:'Zone',derog:'Ausnahmen',req:'Gesuchsteller',lieu:'Ort',
 b_opp:'Einsprache erheben',b_obs:'Bemerkungen einreichen',contact:'Gemeinde kontaktieren',period:'vom {0} bis {1}',period_to:'bis {0}',
 l_inq:'Öffentliche Auflage {0} · Einsprachen bis {1}',l_ge:'Eingereicht am {0} · Bemerkungen: 30 Tage ab Publikation im Amtsblatt (FAO)',l_pub:'Publiziert am {0} · Einsprachen bis {1}',l_approx:' · ungefähre Lage (Gemeindezentrum)',
 ec_b:'Kopieren und eConstruction öffnen ↗',ec_t:'Kopiert die Nummer {0} und öffnet eConstruction (SwissID-Anmeldung nötig)',ec_ok:'Nummer {0} kopiert. Melden Sie sich bei eConstruction mit SwissID an, wählen Sie «Einsicht Amtsblatt» und fügen Sie die Nummer in die Suche ein.',ec_ko:'Notieren Sie die Nummer {0}: Melden Sie sich bei eConstruction mit SwissID an, wählen Sie «Einsicht Amtsblatt» und geben Sie die Nummer in die Suche ein.',
 admin:n=>'Gemeindeverwaltung '+n,tel:'Tel. ',mail:'E-Mail ',hours:'Öffnungszeiten: {0}',site:'Website der Gemeinde ↗',site_h:'Öffnungszeiten und Kontakt auf der Website der Gemeinde ↗',na:'Keine Kontaktangaben verfügbar.',
 notice:'Amtliche Publikation ↗',ref:'Bezugspunkt',n_avis:'{0} Publ.',locality:n=>'Ortschaft von '+n,notfound:'Gemeinde nicht gefunden.',unavail:'Suche nicht verfügbar.',geo_na:'Standortbestimmung auf diesem Gerät nicht verfügbar.',locating:'Standort wird bestimmt…',denied:'Zugriff auf den Standort verweigert: bitte im Browser erlauben.',pos_na:'Standort zurzeit nicht verfügbar.',
 me:'Mein Standort',near:n=>'bei '+n,
 d_wish:'Ich möchte',d_opp:'Einsprache erheben',d_res:'eine privatrechtliche Rechtsverwahrung anmelden',f_prenom:'Vorname',f_nom:'Name',f_adr:'Adresse',f_loc:'PLZ und Ort',f_mail:'E-Mail',opt:'(optional)',f_tel:'Telefon',f_qual:'Einsprachelegitimation',choose:'Auswählen…',
 q1:'Eigentümer/in einer Nachbarparzelle',q2:'Mieter/in oder Bewohner/in in unmittelbarer Nähe',q3:'Eigentümer/in oder Bewohner/in, betroffen von den Auswirkungen des Projekts',q4:'andere',
 f_prec:'Präzisierung',f_prec2:'(Parzelle, Distanz…)',ph_prec:'z. B. Parzelle Nr. 1234, 20 m vom Projekt',f_mot:'Begründung',ph_mot:'Erklären Sie, inwiefern Sie das Projekt persönlich betrifft: Höhe, Schatten, Aussicht, Lärm, Verkehr, Zonenkonformität, Ausnahmen…',
 fine:'Es wird nichts versendet: Der Brief wird in Ihrem Browser erstellt. Ihre Angaben werden nur auf diesem Gerät gespeichert. Unverbindliche Vorlage, die keine Rechtsberatung ersetzt.',cancel:'Abbrechen',gen:'Brief erstellen (drucken / PDF)',close:'Schliessen',
 r_inq:'Öffentliche Auflage {0}',r_dos:'Dossier {0}, eingereicht am {1}',r_pub:'Publiziert am {0}',
 dl_ge:'Frist: 30 Tage ab Publikation des Gesuchs im Amtsblatt (Feuille d’avis officielle). Prüfen Sie das Publikationsdatum auf fao.ge.ch.',dl:'Spätestens am {0} einreichen (Datum des Poststempels)',dl_today:': das ist heute!',dl_left:n=>': noch '+n+' Tag'+(n>1?'e':'')+'.',
 h_consult:'Sehen Sie das Dossier vorher bei der Gemeindeverwaltung ein ({0}){1}.',h_online:' oder online (Link der Publikation)',h_ec:' oder online auf eConstruction',h_print:'Erstellen Sie den Brief, drucken Sie ihn aus (oder speichern Sie ihn als PDF) und unterschreiben Sie ihn von Hand.',h_send:'Senden Sie ihn vorzugsweise eingeschrieben an: {0}.',h_ecdep:'Oder reichen Sie ihn als PDF auf eConstruction ein (Nr. {0}): Dazu sind eine qualifizierte elektronische Signatur und eine verifizierte Identität nötig. ',h_lang:'Der Brief ist auf {0} verfasst, in der Sprache der amtlichen Publikation.',
 lname:{fr:'Französisch',de:'Deutsch'},
 foot:'Quellen: VS Amtsblatt des Kantons Wallis; BE Amtsblatt des Kantons Bern (französischsprachige Publikationen); VD CAMAC, Geodaten des Kantons Waadt; GE SITG; FR Suchmaschine des Staates Freiburg; NE SITN; JU Journal officiel (PDF). Erstellt am {0} (Publikationen der letzten {1} Tage). Die Links öffnen die amtliche Publikation oder das Dossier: Massgebend ist allein die amtliche Publikation. GE: Das Publikationsdatum in der FAO ist nicht verfügbar; die Frist für Bemerkungen beträgt 30 Tage ab dieser Publikation.'},
it:{title:'Pubblicazioni edilizie',home:'Intorno a',ph_home:'Comune o NPA',gps:'Usa la mia posizione',radius:'Raggio: {0} km',search:'Cerca',ph_search:'Comune, parola chiave, particella, richiedente…',canton:'Cantone',type:'Tipo',all:'Tutti',late:'Includi termini scaduti',lang:'Lingua',
 h1:(n,r)=>n+' '+(n===1?'pubblicazione':'pubblicazioni')+' entro '+r+' km',sub:'Intorno a {0} · {1} avvisi ({2}) degli ultimi {3} giorni',empty:'Nessuna pubblicazione in questo raggio.',
 late_b:'scaduto',last:'ultimo giorno',left:'ancora {0} g.',running:'in corso',nature:'Natura dei lavori',camac:'N. CAMAC',dossier:'N. incarto',parcelle:'Particella',zone:'Zona',derog:'Deroghe',req:'Richiedente',lieu:'Luogo',
 b_opp:'Fare opposizione',b_obs:'Inviare osservazioni',contact:'Contattare il comune',period:'dal {0} al {1}',period_to:'fino al {0}',
 l_inq:'Pubblicazione {0} · opposizioni entro il {1}',l_ge:'Depositata il {0} · osservazioni: 30 giorni dalla pubblicazione nel FAO',l_pub:'Pubblicato il {0} · opposizioni entro il {1}',l_approx:' · posizione approssimativa (centro del comune)',
 ec_b:'Copia e apri eConstruction ↗',ec_t:'Copia il numero {0} e apre eConstruction (accesso SwissID necessario)',ec_ok:'Numero {0} copiato. Su eConstruction, accedi con SwissID, poi «Consultazione del Bollettino ufficiale» e incollalo nella ricerca.',ec_ko:'Annota il numero {0}: su eConstruction, accedi con SwissID, poi «Consultazione del Bollettino ufficiale» e inseriscilo nella ricerca.',
 admin:n=>'Amministrazione comunale di '+n,tel:'Tel. ',mail:'E-mail ',hours:'Orari: {0}',site:'Sito del comune ↗',site_h:'Orari e contatti sul sito del comune ↗',na:'Contatti non disponibili.',
 notice:'Avviso ufficiale ↗',ref:'Punto di riferimento',n_avis:'{0} avvisi',locality:n=>'località di '+n,notfound:'Comune non trovato.',unavail:'Ricerca non disponibile.',geo_na:'Geolocalizzazione non disponibile su questo dispositivo.',locating:'Localizzazione…',denied:'Accesso alla posizione negato: autorizzalo nel browser.',pos_na:'Posizione non disponibile al momento.',
 me:'La mia posizione',near:n=>'vicino a '+n,
 d_wish:'Desidero',d_opp:'presentare opposizione',d_res:'formulare una riserva di diritto privato',f_prenom:'Nome',f_nom:'Cognome',f_adr:'Indirizzo',f_loc:'NPA e località',f_mail:'E-mail',opt:'(facoltativo)',f_tel:'Telefono',f_qual:'Legittimazione',choose:'Scegliere…',
 q1:'proprietario/a di una particella vicina',q2:'inquilino/a o abitante nelle immediate vicinanze',q3:'proprietario/a o abitante toccato/a dagli effetti del progetto',q4:'altro',
 f_prec:'Precisazione',f_prec2:'(particella, distanza…)',ph_prec:'es. particella n. 1234, a 20 m dal progetto',f_mot:'Motivi',ph_mot:'Spiega in che modo il progetto ti tocca personalmente: altezza, ombra, vista, rumore, traffico, conformità al regolamento di zona, deroghe…',
 fine:'Nulla viene inviato: la lettera è preparata nel tuo browser. I tuoi dati sono memorizzati solo su questo dispositivo. Modello indicativo, che non sostituisce una consulenza giuridica.',cancel:'Annulla',gen:'Genera la lettera (stampa / PDF)',close:'Chiudi',
 r_inq:'Pubblicazione {0}',r_dos:'Incarto {0}, depositato il {1}',r_pub:'Pubblicato il {0}',
 dl_ge:'Termine: 30 giorni dalla pubblicazione della domanda nel Foglio ufficiale (FAO). Verifica la data di pubblicazione su fao.ge.ch.',dl:'Da inviare al più tardi il {0} (fa fede il timbro postale)',dl_today:': è oggi!',dl_left:n=>': ancora '+n+' giorn'+(n>1?'i':'o')+'.',
 h_consult:'Prima di scrivere, consulta l’incarto presso l’amministrazione comunale ({0}){1}.',h_online:' o online (link dell’avviso)',h_ec:' o online su eConstruction',h_print:'Genera la lettera, stampala (o salvala in PDF) e firmala a mano.',h_send:'Inviala preferibilmente per raccomandata a: {0}.',h_ecdep:'Oppure caricala in PDF su eConstruction (n. {0}): servono una firma elettronica qualificata e un’identità verificata. ',h_lang:'La lettera è redatta in {0}, la lingua dell’avviso ufficiale.',
 lname:{fr:'francese',de:'tedesco'},
 foot:'Fonti: VS Bollettino ufficiale del Vallese; BE Foglio ufficiale del Canton Berna (avvisi in francese); VD CAMAC, geodati del Canton Vaud; GE SITG; FR motore di ricerca dello Stato di Friburgo; NE SITN; JU Journal officiel (PDF). Generato il {0} (avvisi degli ultimi {1} giorni). I link aprono l’avviso o l’incarto ufficiale: fa fede solo la pubblicazione ufficiale. GE: la data di pubblicazione nel FAO non è fornita; il termine per le osservazioni è di 30 giorni da tale pubblicazione.'}};
// Types de procédure et statuts (données en français) → DE / IT
const TYPES={'Autorisation de construire':['Baubewilligung','Licenza edilizia'],'Consultation des plans':['Planauflage','Consultazione dei piani'],'Permis de construire':['Baubewilligung','Licenza edilizia'],
 'Permis de construire (procédure simplifiée)':['Baubewilligung (vereinfachtes Verfahren)','Licenza edilizia (procedura semplificata)'],'Demande définitive':['Definitives Gesuch','Domanda definitiva'],'Demande préalable':['Vorabklärung','Domanda preliminare'],
 'Démolition':['Abbruch','Demolizione'],'Procédure accélérée':['Beschleunigtes Verfahren','Procedura accelerata'],'Démolition (procédure accélérée)':['Abbruch (beschleunigtes Verfahren)','Demolizione (procedura accelerata)'],
 'Avis de construction':['Baupublikation','Avviso di costruzione'],'Sanction définitive':['Definitive Bewilligung','Autorizzazione definitiva'],'Sanction préalable':['Vorentscheid','Decisione preliminare'],'Sanction minime importance':['Bewilligung (geringfügig)','Autorizzazione (lavori minori)'],
 'Instruction':['In Prüfung','In esame'],'Enregistrement':['Registrierung','Registrazione'],'Enregistre':['Registriert','Registrato']};
let LANG=(()=>{try{const l=localStorage.getItem('enq-lang');if(I18N[l])return l;}catch(e){}const n=(navigator.language||'fr').slice(0,2);return I18N[n]?n:'fr';})();
function deFr(n){return (/^[aeiouyàâéèêëîïôûü]/i.test(n||'')?'d’':'de ')+n;}
function T(k,...a){let v=(I18N[LANG]&&I18N[LANG][k]!==undefined)?I18N[LANG][k]:I18N.fr[k];if(typeof v==='function')return v(...a);if(v==null)return k;a.forEach((x,i)=>{v=v.split('{'+i+'}').join(x);});return v;}
function TT(s){const t=TYPES[s];return !s||LANG==='fr'||!t?(s||''):t[LANG==='de'?0:1];}
const LOC={fr:'fr-CH',de:'de-CH',it:'it-CH'};

// ---------- Lettre : rédigée dans la langue de l'avis (FR ou DE), pas celle de l'interface ----------
const LT={
fr:{rec:'Recommandé',date:(lieu,d)=>(lieu?lieu+', ':'')+'le '+d,what_plans:'la mise en consultation publique des plans',what_vd:'la demande de permis de construire',what_ge:'la requête en autorisation de construire',what:'la demande d’autorisation de construire',
 obj_opp:'Opposition à {0}',obj_res:'Réserve de droit privé : {0}',obj_obs:'Observations sur {0}',commune:'Commune',parcelle:'Parcelle',req:'Requérant',inq:'Enquête publique',pub:'Publication',dos:'N° de dossier',camac:'N° CAMAC',ec:'N° eConstruction',
 period:'du {0} au {1}',pub_vs:'Bulletin officiel du Valais du {0}',pub_off:'publication officielle du {0}',no:', n° {0}',
 salut:'Madame, Monsieur,',salut_vd:'Madame la Syndique, Monsieur le Syndic, Mesdames et Messieurs les Municipaux,',
 dl_vs:'dans le délai de 30 jours dès sa publication au Bulletin officiel du Valais',dl_ge:'dans le délai de 30 jours dès sa publication dans la Feuille d’avis officielle',dl_inq:'dans le délai de l’enquête publique',
 b_obs:'Par la présente, et {0}, je vous adresse mes observations au sujet de {1} citée en référence.',b_opp:'Par la présente, et {0}, je forme opposition à {1} citée en référence.',b_res:'Par la présente, et {0}, je formule une réserve de droit privé à l’égard de {1} citée en référence.',
 qual:'Qualité pour agir : {0}. Je suis ainsi directement et personnellement touché·e par ce projet.',qual_is:'je suis {0}',see:'voir motifs ci-dessous',motifs:'Motifs',
 c_obs:'Je vous prie de bien vouloir tenir compte de ces observations dans l’examen de la requête.',c_opp:'Pour ces motifs, je vous prie de bien vouloir rejeter le projet tel qu’il a été mis à l’enquête.',
 close:'Je vous remercie de me tenir informé·e de la suite donnée à la présente et vous prie d’agréer, Madame, Monsieur, mes salutations distinguées.',sig:'Signature',
 muni:n=>'Municipalité '+deFr(n)+'\nGreffe municipal',adm:n=>'Administration communale '+deFr(n),todo_g:'[adresse du greffe municipal à compléter]',todo:'[adresse à compléter]'},
de:{rec:'Einschreiben',date:(lieu,d)=>(lieu?lieu+', ':'')+d,what_plans:'die öffentliche Planauflage',what_vd:'das Baugesuch',what_ge:'das Baugesuch',what:'das Baugesuch',
 obj_opp:'Einsprache gegen {0}',obj_res:'Privatrechtliche Rechtsverwahrung: {0}',obj_obs:'Bemerkungen zu {0}',commune:'Gemeinde',parcelle:'Parzelle',req:'Gesuchsteller',inq:'Öffentliche Auflage',pub:'Publikation',dos:'Dossier-Nr.',camac:'CAMAC-Nr.',ec:'eConstruction-Nr.',
 period:'vom {0} bis {1}',pub_vs:'Amtsblatt des Kantons Wallis vom {0}',pub_off:'amtliche Publikation vom {0}',no:', Nr. {0}',
 salut:'Sehr geehrte Damen und Herren',salut_vd:'Sehr geehrte Damen und Herren',
 dl_vs:'innert der Frist von 30 Tagen seit der Publikation im Amtsblatt des Kantons Wallis',dl_ge:'innert der Frist von 30 Tagen seit der Publikation',dl_inq:'innert der Auflagefrist',
 b_obs:'Hiermit reiche ich {0} meine Bemerkungen zu {1} (siehe oben) ein.',b_opp:'Hiermit erhebe ich {0} Einsprache gegen {1} (siehe oben).',b_res:'Hiermit melde ich {0} eine privatrechtliche Rechtsverwahrung zu {1} (siehe oben) an.',
 qual:'Einsprachelegitimation: {0}. Ich bin somit direkt und persönlich vom Projekt betroffen.',qual_is:'Ich bin {0}',see:'siehe Begründung',motifs:'Begründung',
 c_obs:'Ich bitte Sie, diese Bemerkungen bei der Prüfung des Gesuchs zu berücksichtigen.',c_opp:'Aus diesen Gründen beantrage ich, das Gesuch in der aufgelegten Form abzuweisen.',
 close:'Ich danke Ihnen, mich über das weitere Verfahren zu informieren, und grüsse Sie freundlich.',sig:'Unterschrift',
 muni:n=>'Gemeinderat '+n,adm:n=>'Gemeindeverwaltung '+n,todo_g:'[Adresse der Gemeindekanzlei ergänzen]',todo:'[Adresse ergänzen]'}};
function LTx(L,k,...a){let v=LT[L][k];if(typeof v==='function')return v(...a);a.forEach((x,i)=>{v=v.split('{'+i+'}').join(x);});return v;}

const $=s=>document.querySelector(s), DAY=864e5;
const today=new Date(CFG.today+'T00:00:00');
let fmt,fmtY;function mkfmt(){fmt=new Intl.DateTimeFormat(LOC[LANG],{day:'numeric',month:'long'});fmtY=new Intl.DateTimeFormat(LOC[LANG],{day:'numeric',month:'long',year:'numeric'});}mkfmt();
const fmtL=L=>new Intl.DateTimeFormat(LOC[L],{day:'numeric',month:'long',year:'numeric'});
const pd=s=>s?new Date(s+'T00:00:00'):null;
let home={lat:CFG.home[0],lon:CFG.home[1],label:CFG.home[2]};
try{const s=JSON.parse(localStorage.getItem('enq-home')||'null');if(s&&s.lat)home=s;}catch(e){}
let radius=CFG.rayon;try{const r=+localStorage.getItem('enq-r');if(r>=1&&r<=10)radius=r;}catch(e){}

function km(a,b){const R=6371,r=Math.PI/180,dp=(b.lat-a.lat)*r,dl=(b.lon-a.lon)*r;
 const h=Math.sin(dp/2)**2+Math.cos(a.lat*r)*Math.cos(b.lat*r)*Math.sin(dl/2)**2;return 2*R*Math.asin(Math.sqrt(h));}
DATA.forEach(d=>{const now=new Date();const end=pd(d.fin_delai);d.left=end?Math.round((end-new Date(now.getFullYear(),now.getMonth(),now.getDate()))/DAY):null;});
const isLate=d=>d.left!=null&&d.left<0;

const map=L.map('map',{scrollWheelZoom:true}).setView([home.lat,home.lon],11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
const circle=L.circle([home.lat,home.lon],{radius:radius*1000,color:'#d52b1e',weight:1,fillOpacity:.04}).addTo(map);
const me=L.circleMarker([home.lat,home.lon],{radius:7,color:'#14181d',weight:2,fillColor:'#fff',fillOpacity:1}).addTo(map).bindTooltip(T('ref'));
const layer=L.layerGroup().addTo(map);const markers={};

function el(t,c,x){const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=x;return n;}
function card(d){
 const a=el('article','c');a.dataset.id=d.id;
 const h=el('div','h');h.append(el('span','d',d.dist.toFixed(1)+' km'),el('span','com',d.commune||'?'),el('span','tp kt',d.canton),el('span','tp',TT(d.type)));
 const b=d.left==null?el('span','b',TT(d.statut)||T('running')):el('span','b '+(d.left<0?'late':d.left<=7?'hot':''),d.left<0?T('late_b'):d.left===0?T('last'):T('left',d.left));h.append(b);a.append(h);
 const h3=el('h3');const l=el('a',null,d.projet||d.titre||'—');l.href=d.url;l.target='_blank';l.rel='noopener';h3.append(l);a.append(h3);
 const dl=el('dl');[[T('nature'),'nature'],[T('camac'),'camac'],[T('dossier'),['GE','NE','FR'].includes(d.canton)?'numero':''],[T('lieu'),'lieu'],[T('parcelle'),'parcelle'],[T('zone'),'zone'],[T('derog'),'derogations'],[T('req'),'requerant']]
  .forEach(([k,v])=>{if(v&&d[v]&&!(v==='nature'&&d[v]===d.projet)){dl.append(el('dt',null,k),el('dd',null,d[v]));}});
 if(d.econstruction){const dd=el('dd','ec');dd.append(el('code',null,d.econstruction),ecButton(d.econstruction));dl.append(el('dt',null,'eConstruction'),dd);}
 if(dl.children.length)a.append(dl);
 const end=pd(d.fin_delai);
 const acts=el('div','acts');if(!isLate(d)&&!(d.canton==='GE'&&/accélérée/i.test(d.type||''))){const ob=el('button','oppb',d.canton==='GE'?T('b_obs'):T('b_opp'));ob.type='button';ob.addEventListener('click',e=>{e.stopPropagation();openOpp(d);});acts.append(ob);}
 a.append(el('small',null,(d.canton==='GE'?T('l_ge',fmtY.format(pd(d.date)))
  :d.debut?T('l_inq',T('period',fmt.format(pd(d.debut)),fmtY.format(end)),fmtY.format(end))
  :T('l_pub',fmt.format(pd(d.date)),end?fmtY.format(end):'?'))+(d.coords_exactes?'':T('l_approx'))));
 acts.append(communeDetails(d));
 a.append(acts);
 a.addEventListener('click',e=>{if(e.target.closest('a'))return;const m=markers[d.id];if(m){map.setView(m.getLatLng(),Math.max(map.getZoom(),15));m.openPopup();}select([d.id]);});
 return a;
}
// eConstruction n'a pas de lien public direct vers un dossier : connexion SwissID puis recherche par numéro.
// On copie le numéro et on ouvre la plateforme dans un nouvel onglet.
const EC_URL=CFG.econstruction;let toastT;
function toast(nodes,ms){const t=$('#toast');t.textContent='';t.append(...nodes);t.hidden=false;clearTimeout(toastT);toastT=setTimeout(()=>t.hidden=true,ms||9000);}
function copy(txt){if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(txt);
 return new Promise((ok,ko)=>{const ta=el('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';document.body.append(ta);ta.select();try{document.execCommand('copy')?ok():ko();}catch(e){ko(e);}ta.remove();});}
function ecButton(no){const b=el('button','ecb',T('ec_b'));b.type='button';
 b.title=T('ec_t',no);
 b.addEventListener('click',e=>{e.stopPropagation();window.open(EC_URL,'_blank','noopener');
  const msg=ok=>[document.createTextNode(T(ok?'ec_ok':'ec_ko',no))];
  copy(no).then(()=>toast(msg(true)),()=>toast(msg(false),15000));});
 return b;}
// ---------- Coordonnées de l'administration communale ----------
function communeBox(d){
 const c=COMMUNES[d.bfs]||{nom:d.commune};const box=el('div','box');
 box.append(el('b',null,c.service||T('admin',c.nom||d.commune)));
 if(c.adresse)box.append(el('div',null,c.adresse));
 if(c.telephone){const r=el('div',null,T('tel'));const a=el('a',null,c.telephone);a.href='tel:'+c.telephone.replace(/[^\d+]/g,'');r.append(a);box.append(r);}
 if(c.email){const r=el('div',null,T('mail'));const a=el('a',null,c.email.replace(/^mailto:/,''));a.href='mailto:'+c.email.replace(/^mailto:/,'');r.append(a);box.append(r);}
 if(c.horaires)box.append(el('div',null,T('hours',c.horaires)));
 if(c.site){const a=el('a',null,T(c.horaires?'site':'site_h'));a.href=c.site;a.target='_blank';a.rel='noopener';box.append(a);}
 if(!c.adresse&&!c.telephone&&!c.site)box.append(el('div','muted',T('na')));
 return box;
}
function communeDetails(d){const det=el('details','ci');const s=el('summary',null,T('contact'));det.append(s,communeBox(d));
 det.addEventListener('click',e=>e.stopPropagation());return det;}

// ---------- Faire opposition : lettre pré-remplie, à signer et envoyer ----------
const dlg=$('#opp'),of=$('#opp-f');let cur=null;
const MOI=['prenom','nom','adresse','localite','email','tel','qualite','precision'];
function authority(d,L){
 L=L||'fr';const c=(d.contact||'').trim();if(c)return c;
 const k=COMMUNES[d.bfs]||{};const adr=k.adresse?k.adresse.replace(/,\s*/g,'\n'):null;
 if(d.canton==='VD')return LTx(L,'muni',k.nom||d.commune)+'\n'+(adr||LT[L].todo_g);
 return LTx(L,'adm',k.nom||d.commune)+'\n'+(adr||LT[L].todo);}
const letterLang=d=>d.langue==='de'?'de':'fr';
function openOpp(d){
 cur=d;const end=pd(d.fin_delai);
 const r=$('#opp-ref');r.textContent='';r.append(el('b',null,d.projet||d.titre),
  el('div',null,[d.commune,d.parcelle,d.type].filter(Boolean).join(' · ')),
  el('div',null,d.canton==='GE'?T('r_dos',d.numero,fmtY.format(pd(d.date)))
   :d.debut?T('r_inq',T('period',fmtY.format(pd(d.debut)),fmtY.format(end)))+(d.camac?' (CAMAC '+d.camac+')':'')
   :T('r_pub',fmtY.format(pd(d.date)))+(d.numero?' ('+d.numero+')':'')));
 $('#opp-t').textContent=d.canton==='GE'?T('b_obs'):T('b_opp');
 of.querySelector('fieldset').hidden=d.canton==='GE';if(d.canton==='GE')of.elements.kind[0].checked=true;
 $('#opp-dl').textContent=d.canton==='GE'?T('dl_ge'):T('dl',end?fmtY.format(end):'?')+(d.left===0?T('dl_today'):d.left>0?T('dl_left',d.left):'.');
 const how=$('#opp-how');how.textContent='';
 const LL=letterLang(d);
 {const c=COMMUNES[d.bfs]||{};how.append(el('li',null,T('h_consult',[c.nom||d.commune,c.adresse,c.telephone?T('tel')+c.telephone:'',c.horaires?T('hours',c.horaires):''].filter(Boolean).join(', '),d.econstruction?T('h_ec'):d.canton==='GE'||d.canton==='NE'||d.canton==='FR'?T('h_online'):'')));}
 how.append(el('li',null,T('h_print')));
 how.append(el('li',null,T('h_send',authority(d,LL).replace(/\n/g,', '))));
 if(d.econstruction){const l2=el('li',null,T('h_ecdep',d.econstruction));l2.append(ecButton(d.econstruction));how.append(l2);}
 if(LL!==LANG)how.append(el('li',null,T('h_lang',T('lname')[LL])));
 try{const m=JSON.parse(localStorage.getItem('enq-moi')||'{}');MOI.forEach(k=>{if(m[k]!=null&&of.elements[k])of.elements[k].value=m[k];});}catch(e){}
 of.elements.motifs.value='';
 if(dlg.showModal)dlg.showModal();else dlg.setAttribute('open','');
 setTimeout(()=>{const f=MOI.map(k=>of.elements[k]).find(x=>x&&x.required&&!x.value)||of.elements.motifs;f.focus();},50);
}
function closeOpp(){if(dlg.close)dlg.close();else dlg.removeAttribute('open');}
$('#opp-x').addEventListener('click',closeOpp);$('#opp-c').addEventListener('click',closeOpp);
dlg.addEventListener('click',e=>{if(e.target===dlg)closeOpp();});
of.addEventListener('submit',e=>{
 e.preventDefault();if(!of.reportValidity())return;
 const v={};[...MOI,'motifs','kind'].forEach(k=>v[k]=(of.elements[k].value||'').trim());
 try{const m={};MOI.forEach(k=>m[k]=v[k]);localStorage.setItem('enq-moi',JSON.stringify(m));}catch(e){}
 letter(cur,v);closeOpp();setTimeout(()=>window.print(),50);
});
function letter(d,v){
 const LL=letterLang(d),X=(k,...a)=>LTx(LL,k,...a),F=fmtL(LL);
 const L=$('#letter');L.textContent='';L.lang=LL;const P=t=>el('p',null,t);
 const opp=v.kind!=='reserve',plans=/plans/i.test(d.type||''),ge=d.canton==='GE',vd=d.canton==='VD';
 const from=el('div','from');[v.prenom+' '+v.nom,v.adresse,v.localite,v.tel,v.email].filter(Boolean).forEach(t=>from.append(el('div',null,t)));L.append(from);
 const to=el('div','to');to.append(el('div','rec',X('rec')),el('div',null,authority(d,LL)));L.append(to);
 L.append(el('div','date',X('date',v.localite.replace(/^\d{4}\s*/,''),F.format(new Date()))));
 const what=plans?X('what_plans'):vd?X('what_vd'):ge?X('what_ge'):X('what');
 L.append(el('p','obj',X(ge?'obj_obs':opp?'obj_opp':'obj_res',what)+' – '+(d.projet||d.titre)));
 const refs=el('div','refs');[[X('commune'),d.commune],[X('parcelle'),(d.parcelle||'').replace(/^parcelle\s*/i,'')],[X('req'),d.requerant],
  d.debut?[X('inq'),X('period',F.format(pd(d.debut)),F.format(pd(d.fin_delai)))+(d.numero?X('no',d.numero):'')]
  :ge?[X('dos'),d.numero]:[X('pub'),(d.canton==='VS'?X('pub_vs',F.format(pd(d.date))):X('pub_off',F.format(pd(d.date))))+(d.numero?X('no',d.numero):'')],
  [X('camac'),d.camac],[X('ec'),d.econstruction]]
  .forEach(([k,x])=>{if(x)refs.append(el('div',null,k+(LL==='fr'?' : ':': ')+x));});L.append(refs);
 L.append(P(vd?X('salut_vd'):X('salut')));
 const delai=d.canton==='VS'?X('dl_vs'):ge?X('dl_ge'):X('dl_inq');
 L.append(P(X(ge?'b_obs':opp?'b_opp':'b_res',delai,what)));
 const q=v.qualite==='q4'?(v.precision||X('see')):X('qual_is',I18N[LL][v.qualite]+(v.precision?' ('+v.precision+')':''));
 L.append(P(X('qual',q)));
 L.append(el('p','obj',X('motifs')));L.append(el('div','mot',v.motifs));
 if(ge)L.append(P(X('c_obs')));else if(opp)L.append(P(X('c_opp')));
 L.append(P(X('close')));
 const sig=el('div','sig');sig.append(el('div',null,v.prenom+' '+v.nom),el('div','line',X('sig')));L.append(sig);
}

function select(ids){document.querySelectorAll('.c').forEach(c=>c.classList.toggle('on',ids.includes(c.dataset.id)));}

function update(fit){
 $('#rl').textContent=T('radius',radius);$('#r').value=radius;$('#home').value=home.label;
 circle.setLatLng([home.lat,home.lon]).setRadius(radius*1000);me.setLatLng([home.lat,home.lon]);
 const q=$('#q').value.trim().toLowerCase(),t=$('#t').value,k=$('#k').value,late=$('#late').checked;
 DATA.forEach(d=>d.dist=d.lat!=null?km(home,d):1e9);
 const rows=DATA.filter(d=>d.dist<=radius&&(late||!isLate(d))&&(!t||d.type===t)&&(!k||d.canton===k)&&(!q||d.search.includes(q)))
  .sort((a,b)=>a.dist-b.dist||(a.left??99)-(b.left??99));
 const n=rows.length;
 $('#h1').textContent=T('h1',n,radius);
 $('#sub').textContent=T('sub',home.label,DATA.length,CFG.cantons.join(', '),CFG.jours);
 const list=$('#list');list.textContent='';
 if(!n)list.append(el('p','empty',T('empty')));
 rows.forEach(d=>list.append(card(d)));
 layer.clearLayers();for(const k in markers)delete markers[k];
 rows.forEach(d=>{
  const m=L.circleMarker([d.lat,d.lon],{radius:7,color:isLate(d)?'#888':'#d52b1e',weight:2,fillColor:isLate(d)?'#888':'#d52b1e',fillOpacity:d.coords_exactes?.75:.25});
  const p=el('div');p.append(el('b',null,d.commune));p.append(el('div',null,d.projet||d.titre));
  const a=el('a',null,T('notice'));a.href=d.url;a.target='_blank';a.rel='noopener';p.append(a);
  if(d.econstruction){const w=el('div');w.style.marginTop='6px';w.append(ecButton(d.econstruction));p.append(w);}
  m.bindPopup(p);m.on('click',()=>{select([d.id]);const c=document.querySelector('.c[data-id="'+d.id+'"]');if(c)c.scrollIntoView({behavior:'smooth',block:'center'});});
  m.addTo(layer);markers[d.id]=m;});
 if(fit)map.fitBounds(circle.getBounds(),{padding:[10,10]});
 try{localStorage.setItem('enq-home',JSON.stringify(home));localStorage.setItem('enq-r',radius);}catch(e){}
}
DATA.forEach(d=>d.search=[d.commune,d.canton,d.projet,d.titre,d.nature,d.parcelle,d.requerant,d.zone,d.econstruction,d.numero,d.camac].join(' ').toLowerCase());
// listes « Canton » et « Type » construites d'après les données
[...new Set(DATA.map(d=>d.canton))].sort().forEach(c=>$('#k').append(el('option',null,c)));
[...new Set(DATA.map(d=>d.type).filter(Boolean))].sort().forEach(t=>{const o=el('option',null,TT(t));o.value=t;$('#t').append(o);});

$('#r').addEventListener('input',e=>{radius=+e.target.value;update(false);});
$('#r').addEventListener('change',()=>update(true));
['#q','#t','#k','#late'].forEach(s=>$(s).addEventListener('input',()=>update(false)));
// ---------- Choix du point de référence : suggestions de communes + position GPS ----------
const GEO='https://api3.geo.admin.ch/rest/services/api/SearchServer?type=locations&sr=4326&';
const de=n=>(/^[aeiouyàâéèêëîïôûü]/i.test(n||'')?'d\u2019':'de ')+n;
const norm=s=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[-'’]/g,' ').replace(/\s+/g,' ').trim();
const strip=l=>l.replace(/^\d{4}\s*-\s*|\s*\([A-Z]{2}\)$/g,'').trim();
// Communes présentes dans les données : proposées immédiatement, même hors ligne
const LOCAL=(()=>{const m={};DATA.forEach(d=>{if(d.commune&&d.lat!=null&&!m[d.commune])m[d.commune]={lat:d.lat,lon:d.lon,label:d.commune+' ('+(d.canton||'VS')+')',name:d.commune,n:0};if(m[d.commune])m[d.commune].n++;});return Object.values(m);})();
const inp=$('#home'),box=$('#hl'),gps=$('#gps');
let items=[],active=-1,tm,reqId=0;

function hl(label,q){const i=norm(label).indexOf(q);if(!q||i<0)return document.createTextNode(label);
 const f=document.createDocumentFragment();f.append(label.slice(0,i));const mk=el('mark',null,label.slice(i,i+q.length));f.append(mk,label.slice(i+q.length));return f;}
function render(q){
 box.textContent='';active=-1;
 items.forEach((it,i)=>{const li=el('li');li.id='hl-'+i;li.setAttribute('role','option');li.setAttribute('aria-selected',i===active);
  const span=el('span');span.append(hl(it.disp||it.label,q));li.append(span);if(it.n)li.append(el('small',null,T('n_avis',it.n)));else if(it.sub)li.append(el('small',null,it.sub));
  li.addEventListener('mousedown',e=>{e.preventDefault();pick(i);});box.append(li);});
 const open=items.length>0;box.hidden=!open;inp.setAttribute('aria-expanded',open);
 inp.removeAttribute('aria-activedescendant');
}
function move(d){if(!items.length)return;active=active<0?(d>0?0:items.length-1):(active+d+items.length)%items.length;
 box.querySelectorAll('li').forEach((li,i)=>li.setAttribute('aria-selected',i===active));
 inp.setAttribute('aria-activedescendant','hl-'+active);const li=$('#hl-'+active);if(li)li.scrollIntoView({block:'nearest'});}
function close(){box.hidden=true;inp.setAttribute('aria-expanded','false');inp.removeAttribute('aria-activedescendant');}
function pick(i){const it=items[i];if(!it)return;close();gps.classList.remove('on');choose(it);inp.blur();}

function suggest(v){
 const q=norm(v);
 if(q.length<1){items=[];render('');return;}
 const loc=LOCAL.filter(c=>norm(c.name).startsWith(q)||(q.length>2&&norm(c.name).includes(q)))
  .sort((a,b)=>(norm(b.name).startsWith(q))-(norm(a.name).startsWith(q))||b.n-a.n).slice(0,5);
 items=loc;render(q);
 clearTimeout(tm);if(q.length<2)return;const id=++reqId;
 const J=u=>fetch(u).then(r=>r.json()).then(j=>j.results||[]).catch(()=>[]);
 tm=setTimeout(()=>Promise.all([J(GEO+'origins=gg25,zipcode&limit=8&searchText='+encodeURIComponent(v)),
  J(GEO+'origins=gazetteer&limit=15&searchText='+encodeURIComponent(v))]).then(([com,gaz])=>{
  if(id!==reqId)return;
  const seen=new Set(items.map(x=>norm(x.name)+'|'));const num=/^\d/.test(q);
  const rem=com.filter(r=>r.attrs&&r.attrs.lat).map(r=>{const label=r.attrs.label.replace(/<[^>]+>/g,'').trim();return{lat:r.attrs.lat,lon:r.attrs.lon,label,name:strip(label)};})
   .filter(x=>num||norm(x.name).includes(q))   // la recherche floue propose p.ex. « Chur » pour « Charrat »
   .filter(x=>{const k=norm(x.name)+'|'+(/^\d{4}/.test(x.label)?x.label.slice(0,4):'');if(seen.has(k)||seen.has(norm(x.name)+'|'))return false;seen.add(k);return true;});
  // Localités (« Ort ») : villages et anciennes communes fusionnées, p.ex. Charrat → Martigny
  const vil=num?[]:gaz.filter(r=>r.attrs&&r.attrs.lat&&r.attrs.label.includes('<i>Ort</i>')).map(r=>{
    const m=r.attrs.label.replace(/<i>Ort<\/i>\s*/,'').match(/<b>\s*(.*?)\s*<\/b>\s*\(([A-Z]{2})\)\s*-\s*(.*)$/);if(!m)return null;
    const name=m[1].replace(/<[^>]+>/g,'').trim(),kt=m[2],com=m[3].split(',')[0].replace(/\s*\([A-Z]{2}\)\s*$/,'').trim();
    const same=norm(name)===norm(com);return{lat:r.attrs.lat,lon:r.attrs.lon,name,kt,label:same?name+' ('+kt+')':name+' ('+com+', '+kt+')',disp:same?name+' ('+kt+')':name,sub:same?'':T('locality',com)+' ('+kt+')',village:true};})
   .filter(x=>x&&norm(x.name.replace(/\s*\(.*\)\s*$/,'')).includes(q)&&!seen.has(norm(x.name)+'|'))
   .filter(x=>{const k=norm(x.name)+'|';if(seen.has(k))return false;seen.add(k);return true;});
  const pre=x=>norm(x.name).startsWith(q)?0:1,vs=x=>/\(VS\)$/.test(x.label)||x.kt==='VS'?0:1;
  const extra=rem.concat(vil).sort((a,b)=>vs(a)-vs(b)||pre(a)-pre(b));
  const lab=items.length?items.map(x=>x.label):[];items=items.concat(extra).slice(0,8);
  const keep=active>=0&&lab[active];render(q);if(keep){const i=items.findIndex(x=>x.label===keep);if(i>=0){active=i-1;move(1);}}
 }),180);
}
inp.addEventListener('focus',()=>inp.select());
inp.addEventListener('input',()=>{$('#err').textContent='';suggest(inp.value);});
inp.addEventListener('keydown',e=>{
 if(e.key==='ArrowDown'){e.preventDefault();if(box.hidden)suggest(inp.value);else move(1);}
 else if(e.key==='ArrowUp'){e.preventDefault();move(-1);}
 else if(e.key==='Enter'){e.preventDefault();if(items.length&&!box.hidden)pick(Math.max(active,0));else if(inp.value.trim())search(inp.value.trim());}
 else if(e.key==='Escape'){close();inp.value=home.label;inp.blur();}
});
inp.addEventListener('blur',()=>setTimeout(()=>{close();if(document.activeElement!==inp)inp.value=home.label;},120));
function search(v){fetch(GEO+'origins=gg25,zipcode,gazetteer&limit=1&searchText='+encodeURIComponent(v)).then(r=>r.json()).then(j=>{
 const r=(j.results||[]).find(r=>r.attrs&&r.attrs.lat);if(!r){$('#err').textContent=T('notfound');return;}
 const label=r.attrs.label.replace(/<i>.*?<\/i>\s*/,'').replace(/<[^>]+>/g,'').trim();gps.classList.remove('on');choose({lat:r.attrs.lat,lon:r.attrs.lon,label});inp.blur();})
 .catch(()=>{$('#err').textContent=T('unavail');});}

// Centre du village (« Ort » swisstopo) plutôt que centre géométrique de la commune
function choose(s){
 $('#err').textContent='';home={lat:s.lat,lon:s.lon,label:s.label};const mine=home;update(true);
 if(s.village)return;
 const name=strip(s.label);
 fetch(GEO+'origins=gazetteer&limit=15&searchText='+encodeURIComponent(name)).then(r=>r.json()).then(j=>{
  const esc=name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const hit=(j.results||[]).find(r=>r.attrs&&r.attrs.label.includes('<i>Ort</i>')&&new RegExp('<b>\\s*'+esc+'\\s*</b>','i').test(r.attrs.label));
  if(hit&&home===mine){home={lat:hit.attrs.lat,lon:hit.attrs.lon,label:s.label};update(true);}
 }).catch(()=>{});
}

// Position GPS du navigateur, puis nom de la commune via swisstopo
gps.addEventListener('click',()=>{
 if(!('geolocation' in navigator)){$('#err').textContent=T('geo_na');return;}
 gps.setAttribute('aria-busy','true');$('#err').textContent=T('locating');
 navigator.geolocation.getCurrentPosition(p=>{
  gps.removeAttribute('aria-busy');gps.classList.add('on');$('#err').textContent='';
  const lat=p.coords.latitude,lon=p.coords.longitude,pos={lat,lon};
  // Libellé immédiat : commune la plus proche parmi les avis (le service swisstopo peut mettre >10 s à répondre)
  let near=null,nd=1e9;DATA.forEach(d=>{if(d.lat==null||!d.coords_exactes)return;const k=km(pos,d);if(k<nd){nd=k;near=d.commune;}});
  home={lat,lon,label:near&&nd<=5?T('me')+' ('+T('near',near)+')':T('me')};const mine=home;update(true);
  // Puis commune exacte via swisstopo, si la réponse arrive dans les 20 s
  const ctl=('AbortController' in window)?new AbortController():null;if(ctl)setTimeout(()=>ctl.abort(),20000);
  const y=new Date().getFullYear()-1;
  fetch('https://api3.geo.admin.ch/rest/services/api/MapServer/identify?geometryType=esriGeometryPoint&sr=4326&tolerance=0&returnGeometry=false&layers=all:ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill&timeInstant='+y+'&geometry='+lon+','+lat,ctl?{signal:ctl.signal}:{})
   .then(r=>r.json()).then(j=>{const a=(j.results||[])[0];if(a&&home===mine){home={lat,lon,label:T('me')+' ('+a.attributes.gemname+')'};update(false);}})
   .catch(()=>{});
 },err=>{
  gps.removeAttribute('aria-busy');
  $('#err').textContent=T(err.code===1?'denied':'pos_na');
 },{enableHighAccuracy:false,timeout:15000,maximumAge:300000});
});
function applyStatic(){
 document.documentElement.lang=LANG;mkfmt();
 document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=T(e.dataset.i18n));
 document.querySelectorAll('[data-i18n-ph]').forEach(e=>e.placeholder=T(e.dataset.i18nPh));
 document.querySelectorAll('[data-i18n-title]').forEach(e=>{e.title=T(e.dataset.i18nTitle);e.setAttribute('aria-label',e.title);});
 [...$('#t').options].forEach(o=>{if(o.value)o.textContent=TT(o.value);});
 $('#foot').textContent=T('foot',CFG.generated,CFG.jours);document.title=T('title')+' – Suisse romande';
 me.setTooltipContent(T('ref'));
 document.querySelectorAll('.lng button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.l===LANG)));
}
document.querySelectorAll('.lng button').forEach(b=>b.addEventListener('click',()=>{
 LANG=b.dataset.l;try{localStorage.setItem('enq-lang',LANG);}catch(e){}
 if(/^(Ma position|Mein Standort|La mia posizione)$/.test(home.label))home.label=T('me');
 applyStatic();update(false);}));
applyStatic();
update(true);
})();
</script></body></html>"""


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURS = {"Mo": "lu", "Tu": "ma", "We": "me", "Th": "je", "Fr": "ve", "Sa": "sa", "Su": "di", "PH": "jours fériés"}


def horaires_fr(oh):
    """Horaires au format OpenStreetMap (« Mo-Fr 08:00-12:00 ») → texte français lisible."""
    if not oh:
        return ""
    t = re.sub(r"\b(Mo|Tu|We|Th|Fr|Sa|Su|PH)\b", lambda m: JOURS[m.group(1)], oh)
    t = re.sub(r"(\d{2}):(\d{2})", lambda m: f"{int(m.group(1))}h{m.group(2)}", t)
    t = t.replace("-", "–").replace(",", ", ").replace(";", " ·").replace(" off", " fermé")
    return re.sub(r"\s+", " ", t).strip()


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


COMMUNAL = re.compile(r"^(commune|administration communale|ville|municipalit|gemeinde|stadt)", re.I)


def communes_info(items):
    """Coordonnées des administrations communales, par numéro OFS."""
    base = load_json(os.path.join(ROOT, "data", "communes_vs.json"))
    manuel = load_json(os.path.join(ROOT, "data", "communes_vs_manuel.json"))
    # adresse publiée dans les avis par la commune elle-même (la plus fréquente)
    vues = {}
    for it in items:
        if it.get("ro_bfs") and it["ro_bfs"] == it.get("bfs") and COMMUNAL.match(it.get("ro_nom", "")) and it.get("ro_adresse"):
            vues.setdefault(it["bfs"], {}).setdefault((it["ro_nom"], it["ro_adresse"]), 0)
            vues[it["bfs"]][(it["ro_nom"], it["ro_adresse"])] += 1
    out = {}
    for bfs in {it.get("bfs") for it in items if it.get("bfs")}:
        b, m = base.get(bfs, {}), manuel.get(bfs, {})
        r = {"nom": b.get("nom") or next((i["commune"] for i in items if i.get("bfs") == bfs), "")}
        if bfs in vues:
            (nom, adr), _ = max(vues[bfs].items(), key=lambda kv: kv[1])
            r["service"], r["adresse"] = nom, adr
        elif b.get("adresse"):
            r["adresse"] = b["adresse"]
        for k in ("telephone", "email", "site"):
            if b.get(k):
                r[k] = b[k]
        if b.get("horaires_osm"):
            r["horaires"] = horaires_fr(b["horaires_osm"])
        r.update({k: v for k, v in m.items() if v})  # corrections manuelles en dernier
        out[bfs] = {k: v for k, v in r.items() if v}
    return out


def render(items, home, args, generated, cantons=("VS",)):
    data = [{k: it.get(k) for k in ("id", "canton", "numero", "type", "titre", "projet", "date", "debut", "fin_delai",
                                     "commune", "parcelle", "zone", "derogations", "requerant", "econstruction",
                                     "nature", "camac", "statut", "coords_exactes", "lat", "lon", "url", "contact", "bfs",
                                     "lieu", "consultation", "langue")
             if it.get(k) not in (None, "")}
            for it in items if it.get("lat") is not None]
    cfg = {"home": list(home), "rayon": min(10, max(1, int(round(args.rayon)) or 2)), "jours": args.jours, "econstruction": ECONSTRUCTION_URL, "cantons": list(cantons), "generated": generated,
           "today": dt.date.today().isoformat()}
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__DATA__", js(data)).replace("__CFG__", js(cfg))
            .replace("__COMMUNES__", js(communes_info(items)))
            .replace("__GENERATED__", html.escape(generated)).replace("__JOURS__", str(args.jours))
            .replace("__DELAI__", str(DELAI_OPPOSITION_J)))


# ---------- Vaud et Genève : contours des communes ----------
VD_ARCGIS = "https://www.ogc.vd.ch/public/rest/services/OGC/wmsVD/MapServer"
GE_ARCGIS = "https://vector.sitg.ge.ch/arcgis/rest/services"
GE_OAC = ("Département du territoire\nOffice des autorisations de construire\n"
          "Rue David-Dufour 5\nCase postale 22\n1211 Genève 8")
VD_URL_CAMAC = "https://www.actis.vd.ch/rest/exp/idqry/9008/param/{no}"


def arcgis_query(url, where, fields="*", extra=None, page=1000):
    """Interroge une couche ArcGIS REST page par page ; renvoie les entités (WGS84)."""
    out, offset = [], 0
    while True:
        params = {"where": where, "outFields": fields, "outSR": 4326, "f": "json",
                  "resultOffset": offset, "resultRecordCount": page, **(extra or {})}
        d = json.loads(get(url + "/query", params, accept="application/json"))
        if "error" in d:
            raise RuntimeError(f"{url} : {d['error']}")
        feats = d.get("features") or []
        out.extend(feats)
        if not d.get("exceededTransferLimit") or not feats:
            return out
        offset += len(feats)


def rings_of(geom):
    return [r for r in (geom or {}).get("rings", []) if len(r) > 2]


def in_ring(x, y, ring):
    inside, n = False, len(ring)
    for k in range(n):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class Communes:
    """Attribue une commune (nom, numéro OFS) à un point, à partir de polygones simplifiés."""
    def __init__(self, feats, name_field, bfs_field):
        self.polys = []
        for f in feats:
            rings = rings_of(f.get("geometry"))
            if not rings:
                continue
            xs = [p[0] for r in rings for p in r]
            ys = [p[1] for r in rings for p in r]
            a = f["attributes"]
            self.polys.append((min(xs), min(ys), max(xs), max(ys), rings,
                               str(a.get(name_field) or "").strip(), str(a.get(bfs_field) or "")))

    def find(self, lat, lon):
        best = None
        for x0, y0, x1, y1, rings, name, bfs in self.polys:
            if x0 <= lon <= x1 and y0 <= lat <= y1:
                n = sum(in_ring(lon, lat, r) for r in rings)
                if n % 2 == 1:
                    return name, bfs
                best = best or (name, bfs)
        return best or ("", "")


def parse_ch_date(s):
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s or "")
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def collect_vd(start, today):
    """Vaud : couche CAMAC « Demandes de permis de construire (enquête publique) »."""
    coms = Communes(arcgis_query(f"{VD_ARCGIS}/244", "1=1", "NOM_MIN,NO_COM_FED",
                                 {"maxAllowableOffset": 0.0003, "geometryPrecision": 5}), "NOM_MIN", "NO_COM_FED")
    months, d = [], start.replace(day=1)
    while d <= today + dt.timedelta(days=62):
        months.append(f"DATE_ENQUETE LIKE '%au __.{d.month:02d}.{d.year}'")
        d = (d + dt.timedelta(days=32)).replace(day=1)
    where = "ETAT='Enquête publique en cours' OR " + " OR ".join(months)
    feats = arcgis_query(f"{VD_ARCGIS}/537", where)
    items = []
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        if "x" not in g:
            continue
        m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4}).*?(\d{1,2}\.\d{1,2}\.\d{4})", a.get("DATE_ENQUETE") or "")
        debut, fin = (parse_ch_date(m.group(1)), parse_ch_date(m.group(2))) if m else (None, None)
        if not fin or fin < start:
            continue
        nom, bfs = coms.find(g["y"], g["x"])
        no = a.get("NO_CAMAC")
        items.append({
            "id": f"vd-{no or a.get('OBJECTID')}", "canton": "VD", "numero": a.get("NO_FAO") or "",
            "type": "Permis de construire", "nature": a.get("NATURE") or "",
            "titre": a.get("DESCRIPTION") or "", "projet": a.get("DESCRIPTION") or a.get("NATURE") or "",
            "date": (debut or fin).isoformat(), "debut": debut.isoformat() if debut else "",
            "delai": fin.isoformat(), "commune": nom, "bfs": bfs, "camac": str(no or ""),
            "parcelle": "", "zone": "", "derogations": "", "requerant": "", "econstruction": "",
            "contact": "", "coords_exactes": True, "lat": g["y"], "lon": g["x"],
            "url": a.get("URL") or (VD_URL_CAMAC.format(no=no) if no else ""), "statut": a.get("ETAT") or "",
        })
    return items


GE_TYPES = {"DD": "Demande définitive", "DP": "Demande préalable", "M": "Démolition",
            "APA": "Procédure accélérée", "MPA": "Démolition (procédure accélérée)"}


def collect_ge(start, today):
    """Genève : SITG « Autorisation de construire – Dossier » (dossiers enregistrés ou en instruction)."""
    coms = Communes(arcgis_query(f"{GE_ARCGIS}/CAD_COMMUNE/MapServer/0", "1=1", "COMMUNE,NO_COM_FEDERAL",
                                 {"maxAllowableOffset": 0.0002, "geometryPrecision": 5}), "COMMUNE", "NO_COM_FEDERAL")
    where = (f"DATE_DEPOT >= date '{start.isoformat()}' AND STATUT IN ('ENREGISTREMENT','ENREGISTRE','INSTRUCTION')")
    feats = arcgis_query(f"{GE_ARCGIS}/SIT_AUTOR_DOSSIER/FeatureServer/0", where,
                         "ID_DOSSIER,TYPE_DOSSIER,NOM_DOSSIER,STATUT,DATE_DEPOT,DESCRIPTION,LIEN_SAD")
    items = []
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        if "x" not in g:
            continue
        depot = dt.datetime.fromtimestamp(a["DATE_DEPOT"] / 1000, dt.timezone.utc).date() if a.get("DATE_DEPOT") else today
        nom, bfs = coms.find(g["y"], g["x"])
        nom = nom.title() if nom.isupper() else nom
        if nom.startswith("Genève-"):  # la Ville de Genève est découpée en sections cadastrales
            nom = "Genève"
        t = a.get("TYPE_DOSSIER") or ""
        items.append({
            "id": f"ge-{a.get('ID_DOSSIER')}", "canton": "GE", "numero": a.get("ID_DOSSIER") or "",
            "type": GE_TYPES.get(t, a.get("NOM_DOSSIER") or t), "ge_type": t,
            "titre": (a.get("DESCRIPTION") or "").strip(), "projet": (lambda t: t[:1].upper() + t[1:])((a.get("DESCRIPTION") or "").strip()),
            "date": depot.isoformat(), "debut": "", "delai": "", "commune": nom, "bfs": bfs,
            "parcelle": "", "zone": "", "derogations": "", "requerant": "", "econstruction": "",
            "contact": GE_OAC, "coords_exactes": True, "lat": g["y"], "lon": g["x"],
            "url": a.get("LIEN_SAD") or "", "statut": (a.get("STATUT") or "").capitalize(),
        })
    return items


# ---------- Géocodage d'adresses (swisstopo) ----------
def geocode_addr(text):
    """Adresse postale → (lat, lon) via swisstopo (origine « address »), avec cache."""
    if not text:
        return None
    key = "addr|" + text.strip().lower()
    if key in _geo:
        return tuple(_geo[key]) if _geo[key] else None
    res = None
    try:
        d = get_json(GEO, {"searchText": text, "type": "locations", "origins": "address", "limit": 1, "sr": 4326})
        r = (d.get("results") or [None])[0]
        if r and "lat" in r.get("attrs", {}):
            res = (r["attrs"]["lat"], r["attrs"]["lon"])
    except Exception:
        res = None
    _geo[key] = res
    return res


def locate(addr, commune):
    """Position d'une adresse ; à défaut, centre du village/de la commune. → (lat, lon, exacte)"""
    loc = geocode_addr(addr)
    if loc:
        return loc[0], loc[1], True
    loc = geocode(commune, "gg25") if commune else None
    return (loc[0], loc[1], False) if loc else (None, None, False)


def lv95_xy_to_wgs84(e, n):
    return lv95_to_wgs84(f"{e} {n}")


# ---------- Berne (partie francophone) : Feuille officielle via amtsblattportal ----------
def collect_be(a, start, today):
    pubs = list_publications(["BP-BE10"], start, today)
    pubs = [p for p in pubs if (p.get("meta") or {}).get("language") == "fr"]
    items = []

    def job(p):
        pid = p["meta"]["id"]
        try:
            raw = get(f"{API}/publications/{pid}/xml", accept="application/xml")
        except Exception as e:
            return e
        r = _strip_ns(ET.fromstring(raw))
        m, c = r.find("meta"), r.find("content")
        t = lambda path: (c.findtext(path) or "").strip() if c is not None else ""
        commune = _t(c, "localization/municipalityId/term") or t("projectLocation/address/town")
        addr = " ".join(x for x in (t("projectLocation/address/street"), t("projectLocation/address/houseNumber"),
                                    t("projectLocation/address/swissZipCode"), t("projectLocation/address/town")) if x)
        lat, lon, exact = locate(addr, commune)
        parc = re.search(r"parcelle[^,;]*", t("locationCoordinate"), re.I)
        return {
            "id": pid, "canton": "BE", "numero": m.findtext("publicationNumber") or "",
            "type": "Permis de construire", "titre": _t(m, "title"),
            "projet": clean_contact(t("projectDescription")).replace("\n", " ") or _t(m, "title"),
            "date": (m.findtext("publicationDate") or "")[:10],
            "delai": t("entryDeadline")[:10] or ((dt.date.fromisoformat((m.findtext("publicationDate") or "")[:10])
                      + dt.timedelta(days=int(t("daysAfterPublication") or 30))).isoformat()
                     if re.match(r"\d{4}-\d\d-\d\d", m.findtext("publicationDate") or "") else ""),
            "commune": commune, "bfs": c.findtext("localization/municipalityId/key") if c is not None else "",
            "parcelle": parc.group(0) if parc else "", "zone": t("useZoneSuperstructureRegulations"),
            "derogations": clean_contact(t("exemptions")).replace("\n", " "), "requerant": "",
            "econstruction": "", "lieu": addr,
            "contact": clean_contact(t("registrationOffice")).replace(", ", "\n") or "\n".join(x for x in (
                m.findtext("registrationOffice/displayName"),
                " ".join(y for y in (m.findtext("registrationOffice/street"), m.findtext("registrationOffice/streetNumber")) if y),
                " ".join(y for y in (m.findtext("registrationOffice/swissZipCode"), m.findtext("registrationOffice/town")) if y)) if x),
            "consultation": clean_contact(t("locationCirculationOffice")).replace("\n", " "),
            "coords_exactes": exact, "lat": lat, "lon": lon,
            "url": DETAIL_URL_BE.format(id=pid), "langue": "fr",
        }

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for res in ex.map(job, pubs):
            if isinstance(res, Exception):
                print(f"  ! BE : {res}")
            else:
                items.append(res)
    return items


DETAIL_URL_BE = "https://www.amtsblatt.be.ch/#!/search/publications/detail/{id}"


# ---------- Neuchâtel : SITN, « autorisations de construire – pendant l'enquête » ----------
SITN_WFS = "https://sitn.ne.ch/services/wms?MAP=services&SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature&TYPENAME="


def gml_features(url):
    root = _strip_ns(ET.fromstring(get(url, accept="application/xml")))
    out = []
    for fm in root.iter("featureMember"):
        for f in fm:
            props = {c.tag: (c.text or "").strip() for c in f if c.tag not in ("geom", "boundedBy")}
            g = f.find("geom")
            props["_pos"] = [list(map(float, (p.text or "").split())) for p in g.iter("pos")] if g is not None else []
            props["_rings"] = []
            if g is not None:
                for pl in g.iter("posList"):
                    v = list(map(float, (pl.text or "").split()))
                    props["_rings"].append([[v[k], v[k + 1]] for k in range(0, len(v) - 1, 2)])
            out.append(props)
    return out


def collect_ne(start, today):
    coms = gml_features(SITN_WFS + "la3_limites_communales")
    polys = Communes([{"geometry": {"rings": c["_rings"]}, "attributes": {"n": c.get("comnom"), "b": c.get("nufeco")}}
                      for c in coms], "n", "b")   # coordonnées LV95
    items = []
    for f in gml_features(SITN_WFS + "at034_autorisation_construire_pendant"):
        if not f["_pos"]:
            continue
        e, n = f["_pos"][0][:2]
        ll = lv95_xy_to_wgs84(e, n)
        if not ll:
            continue
        fin = f.get("fin_enquete", "")[:10]
        if not fin or dt.date.fromisoformat(fin) < start:
            continue
        nom, bfs = polys.find(n, e)
        lien = re.search(r'href="([^"]+)"', html.unescape(f.get("lien_avis", "")))
        items.append({
            "id": f"ne-{f.get('instance_id')}", "canton": "NE", "numero": f.get("instance_id", ""),
            "type": f.get("demande_permis") or "Permis de construire", "nature": f.get("type_construction", ""),
            "titre": f.get("description_ouvrage", ""), "projet": f.get("description_ouvrage") or f.get("type_construction", ""),
            "date": f.get("debut_enquete", "")[:10], "debut": f.get("debut_enquete", "")[:10], "delai": fin,
            "commune": nom or f.get("cadastre", ""), "bfs": bfs, "cadastre": f.get("cadastre", ""),
            "parcelle": "", "zone": "", "derogations": "", "requerant": "", "econstruction": "",
            "lieu": f.get("situation", ""), "contact": "", "coords_exactes": True, "lat": ll[0], "lon": ll[1],
            "url": html.unescape(lien.group(1)) if lien else "", "langue": "fr",
        })
    return items


# ---------- Fribourg : moteur de recherche des mises à l'enquête (fr.ch), commune par commune ----------
FR_PAGE = "https://www.fr.ch/dime/seca/mises-a-lenquete-publique-des-demandes-de-permis-de-construire"
FR_API = "https://www.fr.ch/app/friacpub/get_results"


def collect_fr(a, start, today):
    page = get(FR_PAGE, accept="text/html").decode("utf-8", "replace")
    form = page[page.find("fribourg-applications-form"):]
    form = form[:form.find("</form>")]
    hidden = []
    for m in re.finditer(r"<input[^>]*>", form):
        tag = m.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        if n and "fribourg-applications-autocomplete" not in tag and n.group(1) not in ("commune", "dossier_number"):
            v = re.search(r'value="([^"]*)"', tag)
            hidden.append((n.group(1), v.group(1) if v else ""))
    raw = page[:page.find('name="commune__label"')]
    j = raw.rfind("[{&quot;value")
    communes = json.loads(html.unescape(raw[j:raw.find("]", j) + 1]))

    def per_commune(c):
        got, seen = [], set()
        for pg in range(10):
            q = "?" + "&".join(f"form[{k}]={urllib.parse.quote(v)}" for k, v in
                               hidden + [("commune", c["value"]), ("dossier_number", "")]) + f"&page={pg}"
            try:
                h = json.loads(get(FR_API + q, accept="application/json")).get("html", "")
            except Exception as e:
                return e
            new = 0
            for li in re.findall(r'<li class="element-list.*?</li>', h, re.S):
                idm = re.search(r'name="id-item"[^>]*value="(\d+)"|value="(\d+)" name="id-item"', li)
                iid = (idm.group(1) or idm.group(2)) if idm else None
                if not iid or iid in seen:
                    continue
                seen.add(iid); new += 1
                txt = lambda rx: html.unescape(re.sub(r"<[^>]+>", "", (re.search(rx, li, re.S) or [None, ""])[1])).strip()
                meta = dict((html.unescape(k).strip(), html.unescape(re.sub(r"<[^>]+>", "", v)).strip()) for k, v in
                            re.findall(r'meta__label">(.*?)</span><span class="app-friac-meta__value">(.*?)</span>', li, re.S))
                got.append({"iid": iid, "numero": txt(r'name="title-item"><a[^>]*>(.*?)</a>'),
                            "href": (re.search(r'name="title-item"><a href="([^"]+)"', li) or [None, ""])[1],
                            "desc": txt(r'list__item__description">(.*?)</p>'), "meta": meta})
            if new == 0:
                break
        return got

    items = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, res in zip(communes, ex.map(per_commune, communes)):
            if isinstance(res, Exception):
                print(f"  ! FR {c['label']} : {res}")
                continue
            for r in res:
                per = re.findall(r"\d{1,2}\.\d{1,2}\.\d{4}", r["meta"].get("Période de mise à l'enquête", ""))
                debut, fin = (parse_ch_date(per[0]), parse_ch_date(per[-1])) if per else (None, None)
                if not fin or fin < start:
                    continue
                addr = r["meta"].get("Adresse", "")
                lat, lon, exact = locate(addr, c["label"])
                if lat is None:
                    continue
                items.append({
                    "id": f"fr-{r['iid']}", "canton": "FR", "numero": r["numero"],
                    "type": "Permis de construire" + (" (procédure simplifiée)" if r["numero"].endswith("-S") else ""),
                    "titre": r["desc"], "projet": r["desc"], "date": (debut or fin).isoformat(),
                    "debut": debut.isoformat() if debut else "", "delai": fin.isoformat(),
                    "commune": c["label"], "bfs": str(c["value"]), "parcelle": r["meta"].get("Parcelle", ""),
                    "zone": "", "derogations": "", "requerant": "", "econstruction": "", "lieu": addr,
                    "contact": "", "coords_exactes": exact, "lat": lat, "lon": lon,
                    "url": urllib.parse.urljoin("https://www.fr.ch", r["href"]) if r["href"] else FR_PAGE, "langue": "fr",
                })
    return items


# ---------- Jura : avis de construction du Journal officiel (PDF hebdomadaire) ----------
JU_INDEX = "https://rsju.jura.ch/fr/Sommaire/Journal-officiel/Journaux-officiels-{y}.html"
MOIS = {m: i for i, m in enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                                    "septembre", "octobre", "novembre", "décembre"], 1)}


def fr_date(s):
    m = re.search(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", s or "", re.I)
    return dt.date(int(m.group(3)), MOIS[m.group(2).lower()], int(m.group(1))) if m and m.group(2).lower() in MOIS else None


def collect_ju(start, today):
    import shutil, subprocess, tempfile
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext (poppler-utils) est requis pour lire le Journal officiel jurassien")
    pdfs = []
    for y in sorted({start.year, today.year}):
        idx = get(JU_INDEX.format(y=y), accept="text/html").decode("utf-8", "replace")
        for href, title in re.findall(r'href="([^"]+\.pdf[^"]*)"[^>]*title="(Journal Officiel No[^"]+)"', idx):
            d = parse_ch_date(re.search(r"du (\d{1,2}\.\d{1,2}\.\d{4})", title).group(1)) if re.search(r"du (\d{1,2}\.\d{1,2}\.\d{4})", title) else None
            if d and start <= d <= today:
                pdfs.append((d, html.unescape(href), re.search(r"No (\d+)", title).group(1)))
    items = []
    for jo_date, href, no in sorted(pdfs):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "jo.pdf")
            with open(pdf, "wb") as f:
                f.write(get(href, accept="application/pdf"))
            txt = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
        lines = [l.strip() for l in txt.splitlines()
                 if l.strip() and not re.match(r"(PAGE \d+|JOURNAL OFFICIEL No|journalofficiel@)", l.strip())]
        body = "\n".join(lines)
        k = body.find("Avis de construction")
        if k < 0:
            continue
        sect = body[k + len("Avis de construction"):]
        blocks = re.split(r"\n(?=Requérant)", sect)
        for n_b, b in enumerate(blocks[1:]):
            prev = blocks[n_b].strip().splitlines()
            flat = re.sub(r"\s+", " ", b.replace("-\n", "").replace("\n", " "))
            fin = fr_date((re.search(r"inclusivement fixée au ([^.]+\d{4})", flat) or [None, ""])[1])
            if not fin:
                continue
            desc = (re.search(r"Description du projet : (.*?)(?: Cadastre :| Dimensions(?: et genre de construction)? :|$)", flat) or [None, ""])[1].strip()
            cad = re.search(r"Cadastre : ([^.]+)\. Parcelle No ([\w./-]+),? sise (?:à|au|aux|en) (?:la |le |l’|l'|les )?(.*?)\. Affectation", flat)
            com = re.search(r"[Cc]ommune (?:mixte )?(?:d’|d'|de |du )([A-ZÉÈÂ][^,]+?),", flat)
            commune = (com.group(1).strip() if com else (cad.group(1).strip() if cad else (prev[-1] if prev else "")))
            adresse = cad.group(3) if cad else ""
            dep = re.search(r"Dépôt public de la demande avec plans au (.*?), où les oppositions", flat)
            lat, lon, exact = locate(adresse, commune)
            if lat is None:
                continue
            items.append({
                "id": f"ju-{no}-{n_b}", "canton": "JU", "numero": f"JO {no}/{jo_date.year}",
                "type": "Avis de construction", "titre": desc, "projet": desc,
                "date": jo_date.isoformat(), "delai": fin.isoformat(), "commune": commune, "bfs": "",
                "parcelle": (f"{cad.group(2)} ({cad.group(1).strip()})" if cad else ""),
                "zone": (re.search(r"Affectation de la zone : (.*?)\.(?: [A-Z]|$)", flat) or [None, ""])[1],
                "derogations": (re.search(r"Dérogations? requises? : (.*?)\.(?: [A-Z]|$)", flat) or [None, ""])[1],
                "requerant": (re.search(r"Requérante?s?(?: et auteure? du projet)? : ([^,]+)", flat) or [None, ""])[1],
                "econstruction": "", "lieu": adresse,
                "contact": (dep.group(1).replace("secrétariat de la ", "").replace("Secrétariat de la ", "").replace(", ", "\n")
                            if dep else ""),
                "coords_exactes": exact, "lat": lat, "lon": lon, "url": href, "langue": "fr",
            })
    return items


def collect_vs(a, start, today):
    subs = a.rubrique or list(SUBRUBRIQUES)
    pubs = list_publications(subs, start, today)
    print(f"  {len(pubs)} publications au Bulletin officiel du Valais")
    ids = [(p.get("meta") or p).get("id") for p in pubs]
    ids = list(dict.fromkeys(i for i in ids if i))  # la pagination de l'API peut renvoyer des doublons
    items, done = [], 0

    def job(pid):
        try:
            return fetch_one(pid)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for pid, res in zip(ids, ex.map(job, ids)):
            done += 1
            if isinstance(res, Exception):
                print(f"  ! {pid} : {res}")
                continue
            raw, it = res
            if a.debug and done == 1:
                with open("debug_premier_avis.xml", "wb") as f:
                    f.write(raw)
                print("[debug] XML du premier avis → debug_premier_avis.xml")
            it["canton"] = "VS"
            if it["lat"] is None:
                loc = geocode(it["commune"], "gg25") or geocode(it["npa"], "zipcode")
                if loc:
                    it["lat"], it["lon"] = loc[0], loc[1]
            if not it.get("delai"):
                try:
                    pubd = dt.date.fromisoformat(it["date"])
                except ValueError:
                    pubd = today
                it["delai"] = (pubd + dt.timedelta(days=DELAI_OPPOSITION_J)).isoformat()
            items.append(it)
            if done % 100 == 0:
                print(f"  … {done}/{len(ids)}")
    return items


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Mises à l'enquête publique près de chez toi (VS, VD, GE)")
    ap.add_argument("--home", default="Evionnaz", help="commune ou NPA de référence (défaut : Evionnaz)")
    ap.add_argument("--rayon", type=float, default=2, help="rayon en km, de 1 à 10 (défaut : 2)")
    ap.add_argument("--jours", type=int, default=30, help="publications des N derniers jours (défaut : 30)")
    ap.add_argument("--tous", action="store_true", help="inclure les avis dont le délai est échu")
    ap.add_argument("--cantons", default="VS,VD,GE,FR,NE,JU,BE", help="cantons à inclure (défaut : cantons romands VS,VD,GE,FR,NE,JU et Berne francophone BE)")
    ap.add_argument("--rubrique", action="append", help="VS : forcer une sous-rubrique (ex. BA-VS05) ; répétable")
    ap.add_argument("--out", default="enquetes.html", help="fichier HTML (défaut : enquetes.html)")
    ap.add_argument("--json", help="exporter aussi les données (avis dans le rayon) en JSON")
    ap.add_argument("--workers", type=int, default=8, help="téléchargements en parallèle (défaut : 8)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    _load_cache()
    home = geocode(a.home)
    if not home:
        sys.exit(f"Commune/NPA introuvable : {a.home}")
    today = dt.date.today()
    start = today - dt.timedelta(days=a.jours)
    cantons = [c.strip().upper() for c in a.cantons.split(",") if c.strip()]
    print(f"→ Cantons {cantons}, du {start} au {today}, autour de {home[2]} ({a.rayon:g} km)")

    sources = {"VS": lambda: collect_vs(a, start, today), "VD": lambda: collect_vd(start, today),
               "GE": lambda: collect_ge(start, today), "FR": lambda: collect_fr(a, start, today),
               "NE": lambda: collect_ne(start, today), "JU": lambda: collect_ju(start, today),
               "BE": lambda: collect_be(a, start, today)}
    items, ok = [], []
    for c in cantons:
        if c not in sources:
            print(f"  ! canton non pris en charge : {c}")
            continue
        try:
            got = sources[c]()
            print(f"→ {c} : {len(got)} avis")
            items.extend(got)
            ok.append(c)
        except Exception as e:  # une source en panne ne bloque pas les autres
            print(f"  ! {c} indisponible : {e}")
    _save_cache()
    if not ok:
        sys.exit("Aucune source disponible.")

    # numéro OFS manquant (Jura : seul le nom de la commune figure dans le PDF) → d'après le nom
    import unicodedata
    nrm = lambda t: re.sub(r"[^a-z]", "", unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode().lower())
    par_nom = {}
    for b, r in load_json(os.path.join(ROOT, "data", "communes_vs.json")).items():
        par_nom.setdefault(nrm(r.get("nom")), b)
    for it in items:
        if not it.get("bfs") and it.get("commune"):
            it["bfs"] = par_nom.get(nrm(it["commune"]), "")
    for it in items:
        it.setdefault("canton", "VS")
        if it.get("delai"):
            fin = dt.date.fromisoformat(it["delai"])
            it["fin_delai"], it["jours_restants"] = fin.isoformat(), (fin - today).days
        else:  # Genève : délai non publié dans les données
            it["fin_delai"], it["jours_restants"] = None, None
        it["distance"] = km(home, (it["lat"], it["lon"])) if it.get("lat") is not None else None

    if not a.tous:
        items = [i for i in items if i["jours_restants"] is None or i["jours_restants"] >= 0]
    items.sort(key=lambda i: (i["distance"] if i["distance"] is not None else 1e9,
                              i["jours_restants"] if i["jours_restants"] is not None else 99))
    proches = [i for i in items if i["distance"] is not None and i["distance"] <= a.rayon]

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(render(items, home, a, dt.datetime.now().strftime("%d.%m.%Y %H:%M"), ok))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(proches, f, ensure_ascii=False, indent=2)
    par = ", ".join(f"{c} {sum(1 for i in items if i['canton'] == c)}" for c in ok)
    print(f"✓ {len(proches)} avis à moins de {a.rayon:g} km ({len(items)} au total : {par}) → {os.path.abspath(a.out)}")
    if len(ok) < len([c for c in cantons if c in sources]):
        sys.exit(2)  # une source a manqué : échec volontaire, la page de la veille reste en ligne


if __name__ == "__main__":
    main()
