#!/usr/bin/env python3
"""
enquetes_vs.py — Mises à l'enquête publique près de chez toi (Valais).

Source : Bulletin officiel du Valais, publié via l'API publique du
Portail des feuilles officielles (amtsblattportal.ch, SECO), rubriques
BA-VS05 (demandes d'autorisation de construire) et BA-VS10 (mises en
consultation publique des plans).
Géocodage des communes / NPA : api3.geo.admin.ch (swisstopo).

Aucune dépendance : Python 3.8+ standard.

Exemples :
  python3 enquetes_vs.py                          # Evionnaz, 15 km, 30 jours
  python3 enquetes_vs.py --home Martigny --rayon 8
  python3 enquetes_vs.py --jours 60 --tous        # y c. délais échus
  python3 enquetes_vs.py --json enquetes.json     # export brut en plus
  python3 enquetes_vs.py --debug                  # dump XML du 1er avis

La page générée contient tous les avis du canton : le rayon et la commune
de référence se règlent ensuite directement dans la page.
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
    contact = _t(reaction, "contact") if reaction is not None else ""

    commune = _t(content, "localization/municipalityId/term") if content is not None else ""
    office = meta.findtext("registrationOffice/displayName") or ""
    coords = lv95_to_wgs84(", ".join(fields.get("coordinates", [])))

    g = lambda k: ", ".join(fields.get(k, []))
    return {
        "id": pid,
        "numero": meta.findtext("publicationNumber") or "",
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
<title>Mises à l'enquête en Valais</title>
<meta name="description" content="Les mises à l'enquête publique (constructions et plans) publiées au Bulletin officiel du Valais, près de chez vous.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23d52b1e'/><path d='M13 7h6v6h6v6h-6v6h-6v-6H7v-6h6z' fill='white'/></svg>">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#fff;--ink:#14181d;--mut:#5b6470;--red:#d52b1e;--line:#e3e6ea;--soft:#f5f6f8;--hi:#fff4f3;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--ink:#e8ecf0;--mut:#9aa4ae;--red:#ff5a4c;--line:#2a3038;--soft:#181c21;--hi:#2a1615;color-scheme:dark}}
*{box-sizing:border-box;min-width:0}body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:24px max(4vw,16px) 16px;border-top:6px solid var(--red)}
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
.tp{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:1px 8px}
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
@media (max-width:560px){.ctl{display:grid;grid-template-columns:1fr 1fr}.ctl .fld,.ctl label:nth-child(3),.ctl #err{grid-column:1/-1}#home,#q,input[type=range]{width:100%}#home{flex:1}}
</style></head><body>
<header>
 <h1 id="h1">Mises à l'enquête</h1>
 <div class="sub" id="sub"></div>
 <div class="ctl">
  <div class="fld"><label for="home">Autour de</label><div class="acw"><input id="home" type="text" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="hl" autocomplete="off" spellcheck="false" placeholder="Commune ou NPA"><button id="gps" type="button" title="Utiliser ma position" aria-label="Utiliser ma position"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5" fill="currentColor"/><path d="M12 1v4M12 19v4M1 12h4M19 12h4"/></svg></button><ul id="hl" role="listbox" aria-label="Communes" hidden></ul></div></div>
  <label><span>Rayon : <span id="rv"></span> km</span><input type="range" id="r" min="1" max="60" step="1"></label>
  <label>Rechercher<input id="q" type="search" placeholder="Commune, mot-clé, parcelle, requérant…"></label>
  <label>Type<select id="t"><option value="">Tous</option><option>Autorisation de construire</option><option>Consultation des plans</option></select></label>
  <label class="chk"><input type="checkbox" id="late"> Inclure les délais échus</label>
  <span id="err" role="status"></span>
 </div>
</header>
<div id="toast" role="status" aria-live="polite" hidden></div>
<div class="layout">
 <div id="map"></div>
 <main><section id="list"></section>
 <p class="foot">Source : Bulletin officiel du Valais (amtsblattportal.ch), généré le __GENERATED__ (publications des __JOURS__ derniers jours).
 Le lien de chaque avis ouvre la publication officielle, seule à faire foi. Le délai affiché est celui indiqué dans l'avis
 (à défaut : __DELAI__ jours dès la publication). Les dossiers se consultent sur eConstruction (connexion SwissID, « Consultation du Bulletin Officiel », puis recherche avec le numéro indiqué) ou auprès de la commune.</p></main>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
const DATA=__DATA__, CFG=__CFG__;
const $=s=>document.querySelector(s), DAY=864e5;
const today=new Date(CFG.today+'T00:00:00');
const fmt=new Intl.DateTimeFormat('fr-CH',{day:'numeric',month:'long'});
const fmtY=new Intl.DateTimeFormat('fr-CH',{day:'numeric',month:'long',year:'numeric'});
const pd=s=>s?new Date(s+'T00:00:00'):null;
let home={lat:CFG.home[0],lon:CFG.home[1],label:CFG.home[2]};
try{const s=JSON.parse(localStorage.getItem('enq-home')||'null');if(s&&s.lat)home=s;}catch(e){}
let radius=CFG.rayon;try{const r=+localStorage.getItem('enq-r');if(r)radius=r;}catch(e){}

function km(a,b){const R=6371,r=Math.PI/180,dp=(b.lat-a.lat)*r,dl=(b.lon-a.lon)*r;
 const h=Math.sin(dp/2)**2+Math.cos(a.lat*r)*Math.cos(b.lat*r)*Math.sin(dl/2)**2;return 2*R*Math.asin(Math.sqrt(h));}
DATA.forEach(d=>{const now=new Date();const end=pd(d.fin_delai);d.left=end?Math.round((end-new Date(now.getFullYear(),now.getMonth(),now.getDate()))/DAY):99;});

const map=L.map('map',{scrollWheelZoom:true}).setView([home.lat,home.lon],11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
const circle=L.circle([home.lat,home.lon],{radius:radius*1000,color:'#d52b1e',weight:1,fillOpacity:.04}).addTo(map);
const me=L.circleMarker([home.lat,home.lon],{radius:7,color:'#14181d',weight:2,fillColor:'#fff',fillOpacity:1}).addTo(map).bindTooltip('Point de référence');
const layer=L.layerGroup().addTo(map);const markers={};

function el(t,c,x){const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=x;return n;}
function card(d){
 const a=el('article','c');a.dataset.id=d.id;
 const h=el('div','h');h.append(el('span','d',d.dist.toFixed(1)+' km'),el('span','com',d.commune),el('span','tp',d.type));
 const b=el('span','b '+(d.left<0?'late':d.left<=7?'hot':''),d.left<0?'échu':d.left===0?'dernier jour':'J-'+d.left);h.append(b);a.append(h);
 const h3=el('h3');const l=el('a',null,d.projet||d.titre||'Avis');l.href=d.url;l.target='_blank';l.rel='noopener';h3.append(l);a.append(h3);
 const dl=el('dl');[['Parcelle','parcelle'],['Zone','zone'],['Dérogations','derogations'],['Requérant','requerant']]
  .forEach(([k,v])=>{if(d[v]){dl.append(el('dt',null,k),el('dd',null,d[v]));}});
 if(d.econstruction){const dd=el('dd','ec');dd.append(el('code',null,d.econstruction),ecButton(d.econstruction));dl.append(el('dt',null,'eConstruction'),dd);}
 if(dl.children.length)a.append(dl);
 const end=pd(d.fin_delai);
 a.append(el('small',null,'Publié le '+fmt.format(pd(d.date))+' · oppositions jusqu’au '+(end?fmtY.format(end):'?')+(d.coords_exactes?'':' · position approximative (centre de la commune)')));
 a.addEventListener('click',e=>{if(e.target.closest('a'))return;const m=markers[d.id];if(m){map.setView(m.getLatLng(),Math.max(map.getZoom(),15));m.openPopup();}select([d.id]);});
 return a;
}
// eConstruction n'a pas de lien public direct vers un dossier : connexion SwissID puis recherche par numéro.
// On copie le numéro et on ouvre la plateforme dans un nouvel onglet.
const EC_URL=CFG.econstruction;let toastT;
function toast(nodes,ms){const t=$('#toast');t.textContent='';t.append(...nodes);t.hidden=false;clearTimeout(toastT);toastT=setTimeout(()=>t.hidden=true,ms||9000);}
function copy(txt){if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(txt);
 return new Promise((ok,ko)=>{const ta=el('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';document.body.append(ta);ta.select();try{document.execCommand('copy')?ok():ko();}catch(e){ko(e);}ta.remove();});}
function ecButton(no){const b=el('button','ecb','Copier et ouvrir eConstruction ↗');b.type='button';
 b.title='Copie le numéro '+no+' et ouvre eConstruction (connexion SwissID requise)';
 b.addEventListener('click',e=>{e.stopPropagation();window.open(EC_URL,'_blank','noopener');
  const msg=ok=>[document.createTextNode(ok?'Numéro ':'Notez le numéro '),el('b',null,no),document.createTextNode((ok?' copié. Sur':' : sur')+' eConstruction, connectez-vous avec SwissID, puis « Consultation du Bulletin Officiel » et collez-le dans la recherche.')];
  copy(no).then(()=>toast(msg(true)),()=>toast(msg(false),15000));});
 return b;}
function select(ids){document.querySelectorAll('.c').forEach(c=>c.classList.toggle('on',ids.includes(c.dataset.id)));}

function update(fit){
 $('#rv').textContent=radius;$('#r').value=radius;$('#home').value=home.label;
 circle.setLatLng([home.lat,home.lon]).setRadius(radius*1000);me.setLatLng([home.lat,home.lon]);
 const q=$('#q').value.trim().toLowerCase(),t=$('#t').value,late=$('#late').checked;
 DATA.forEach(d=>d.dist=d.lat!=null?km(home,d):1e9);
 const rows=DATA.filter(d=>d.dist<=radius&&(late||d.left>=0)&&(!t||d.type===t)&&(!q||d.search.includes(q)))
  .sort((a,b)=>a.dist-b.dist||a.left-b.left);
 const n=rows.length;
 $('#h1').textContent=n+' mise'+(n>1?'s':'')+' à l’enquête à moins de '+radius+' km';
 $('#sub').textContent='Autour de '+home.label+' · Bulletin officiel du Valais · '+DATA.length+' avis publiés ces '+CFG.jours+' derniers jours dans le canton';
 const list=$('#list');list.textContent='';
 if(!n)list.append(el('p','empty','Aucune mise à l’enquête ne correspond dans ce rayon.'));
 rows.forEach(d=>list.append(card(d)));
 layer.clearLayers();for(const k in markers)delete markers[k];
 rows.forEach(d=>{
  const m=L.circleMarker([d.lat,d.lon],{radius:7,color:d.left<0?'#888':'#d52b1e',weight:2,fillColor:d.left<0?'#888':'#d52b1e',fillOpacity:d.coords_exactes?.75:.25});
  const p=el('div');p.append(el('b',null,d.commune));p.append(el('div',null,d.projet||d.titre));
  const a=el('a',null,'Avis officiel ↗');a.href=d.url;a.target='_blank';a.rel='noopener';p.append(a);
  if(d.econstruction){const w=el('div');w.style.marginTop='6px';w.append(ecButton(d.econstruction));p.append(w);}
  m.bindPopup(p);m.on('click',()=>{select([d.id]);const c=document.querySelector('.c[data-id="'+d.id+'"]');if(c)c.scrollIntoView({behavior:'smooth',block:'center'});});
  m.addTo(layer);markers[d.id]=m;});
 if(fit)map.fitBounds(circle.getBounds(),{padding:[10,10]});
 try{localStorage.setItem('enq-home',JSON.stringify(home));localStorage.setItem('enq-r',radius);}catch(e){}
}
DATA.forEach(d=>d.search=[d.commune,d.projet,d.titre,d.parcelle,d.requerant,d.zone,d.econstruction,d.numero].join(' ').toLowerCase());

$('#r').addEventListener('input',e=>{radius=+e.target.value;update(false);});
$('#r').addEventListener('change',()=>update(true));
['#q','#t','#late'].forEach(s=>$(s).addEventListener('input',()=>update(false)));
// ---------- Choix du point de référence : suggestions de communes + position GPS ----------
const GEO='https://api3.geo.admin.ch/rest/services/api/SearchServer?type=locations&sr=4326&';
const norm=s=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[-'’]/g,' ').replace(/\s+/g,' ').trim();
const strip=l=>l.replace(/^\d{4}\s*-\s*|\s*\([A-Z]{2}\)$/g,'').trim();
// Communes présentes dans les données : proposées immédiatement, même hors ligne
const LOCAL=(()=>{const m={};DATA.forEach(d=>{if(d.commune&&d.lat!=null&&!m[d.commune])m[d.commune]={lat:d.lat,lon:d.lon,label:d.commune+' (VS)',name:d.commune,n:0};if(m[d.commune])m[d.commune].n++;});return Object.values(m);})();
const inp=$('#home'),box=$('#hl'),gps=$('#gps');
let items=[],active=-1,tm,reqId=0;

function hl(label,q){const i=norm(label).indexOf(q);if(!q||i<0)return document.createTextNode(label);
 const f=document.createDocumentFragment();f.append(label.slice(0,i));const mk=el('mark',null,label.slice(i,i+q.length));f.append(mk,label.slice(i+q.length));return f;}
function render(q){
 box.textContent='';active=-1;
 items.forEach((it,i)=>{const li=el('li');li.id='hl-'+i;li.setAttribute('role','option');li.setAttribute('aria-selected',i===active);
  const span=el('span');span.append(hl(it.label,q));li.append(span);if(it.n)li.append(el('small',null,it.n+' avis'));
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
 tm=setTimeout(()=>fetch(GEO+'origins=gg25,zipcode&limit=8&searchText='+encodeURIComponent(v)).then(r=>r.json()).then(j=>{
  if(id!==reqId)return;
  const seen=new Set(items.map(x=>norm(x.name)+'|'));
  const rem=(j.results||[]).filter(r=>r.attrs&&r.attrs.lat).map(r=>{const label=r.attrs.label.replace(/<[^>]+>/g,'').trim();return{lat:r.attrs.lat,lon:r.attrs.lon,label,name:strip(label)};})
   .filter(x=>{const k=norm(x.name)+'|'+(/^\d{4}/.test(x.label)?x.label.slice(0,4):'');if(seen.has(k)||seen.has(norm(x.name)+'|'))return false;seen.add(k);return true;});
  const lab=items.length?items.map(x=>x.label):[];items=items.concat(rem).slice(0,8);
  const keep=active>=0&&lab[active];render(q);if(keep){const i=items.findIndex(x=>x.label===keep);if(i>=0){active=i-1;move(1);}}
 }).catch(()=>{}),180);
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
function search(v){fetch(GEO+'origins=gg25,zipcode&limit=1&searchText='+encodeURIComponent(v)).then(r=>r.json()).then(j=>{
 const r=(j.results||[]).find(r=>r.attrs&&r.attrs.lat);if(!r){$('#err').textContent='Commune introuvable.';return;}
 const label=r.attrs.label.replace(/<[^>]+>/g,'').trim();gps.classList.remove('on');choose({lat:r.attrs.lat,lon:r.attrs.lon,label});inp.blur();})
 .catch(()=>{$('#err').textContent='Recherche indisponible.';});}

// Centre du village (« Ort » swisstopo) plutôt que centre géométrique de la commune
function choose(s){
 $('#err').textContent='';home={lat:s.lat,lon:s.lon,label:s.label};const mine=home;update(true);
 const name=strip(s.label);
 fetch(GEO+'origins=gazetteer&limit=15&searchText='+encodeURIComponent(name)).then(r=>r.json()).then(j=>{
  const esc=name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const hit=(j.results||[]).find(r=>r.attrs&&r.attrs.label.includes('<i>Ort</i>')&&new RegExp('<b>\\s*'+esc+'\\s*</b>','i').test(r.attrs.label));
  if(hit&&home===mine){home={lat:hit.attrs.lat,lon:hit.attrs.lon,label:s.label};update(true);}
 }).catch(()=>{});
}

// Position GPS du navigateur, puis nom de la commune via swisstopo
gps.addEventListener('click',()=>{
 if(!('geolocation' in navigator)){$('#err').textContent='Géolocalisation non disponible sur cet appareil.';return;}
 gps.setAttribute('aria-busy','true');$('#err').textContent='Localisation…';
 navigator.geolocation.getCurrentPosition(p=>{
  gps.removeAttribute('aria-busy');gps.classList.add('on');$('#err').textContent='';
  const lat=p.coords.latitude,lon=p.coords.longitude;home={lat,lon,label:'Ma position'};const mine=home;update(true);
  const y=new Date().getFullYear()-1;
  fetch('https://api3.geo.admin.ch/rest/services/api/MapServer/identify?geometryType=esriGeometryPoint&sr=4326&tolerance=0&returnGeometry=false&layers=all:ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill&timeInstant='+y+'&geometry='+lon+','+lat)
   .then(r=>r.json()).then(j=>{const a=(j.results||[])[0];if(a&&home===mine){home={lat,lon,label:'Ma position ('+a.attributes.gemname+')'};update(false);}})
   .catch(()=>{});
 },err=>{
  gps.removeAttribute('aria-busy');
  $('#err').textContent=err.code===1?'Accès à la position refusé : autorisez-le dans le navigateur.':'Position indisponible pour le moment.';
 },{enableHighAccuracy:false,timeout:15000,maximumAge:300000});
});
update(true);
})();
</script></body></html>"""


def render(items, home, args, generated):
    data = [{k: it.get(k) for k in ("id", "numero", "type", "titre", "projet", "date", "fin_delai", "commune",
                                     "parcelle", "zone", "derogations", "requerant", "econstruction",
                                     "coords_exactes", "lat", "lon", "url")}
            for it in items if it.get("lat") is not None]
    cfg = {"home": list(home), "rayon": int(round(args.rayon)) or 15, "jours": args.jours, "econstruction": ECONSTRUCTION_URL,
           "today": dt.date.today().isoformat()}
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__DATA__", js(data)).replace("__CFG__", js(cfg))
            .replace("__GENERATED__", html.escape(generated)).replace("__JOURS__", str(args.jours))
            .replace("__DELAI__", str(DELAI_OPPOSITION_J)))


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Mises à l'enquête publique près de chez toi (Valais)")
    ap.add_argument("--home", default="Evionnaz", help="commune ou NPA de référence (défaut : Evionnaz)")
    ap.add_argument("--rayon", type=float, default=15, help="rayon en km (défaut : 15)")
    ap.add_argument("--jours", type=int, default=30, help="publications des N derniers jours (défaut : 30)")
    ap.add_argument("--tous", action="store_true", help="inclure les avis dont le délai est échu")
    ap.add_argument("--rubrique", action="append", help="forcer une sous-rubrique (ex. BA-VS05) ; répétable")
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

    subs = a.rubrique or list(SUBRUBRIQUES)
    print(f"→ Sous-rubriques {subs}, du {start} au {today}, autour de {home[2]} ({a.rayon:g} km)")
    pubs = list_publications(subs, start, today)
    print(f"→ {len(pubs)} publications en Valais")
    if not pubs:
        print("Aucun résultat. Essaie --debug, ou --rubrique avec un autre code.")

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
                print("[debug] champs extraits :", json.dumps(it, ensure_ascii=False, indent=1))
            if it["lat"] is None:
                loc = geocode(it["commune"], "gg25") or geocode(it["npa"], "zipcode")
                if loc:
                    it["lat"], it["lon"] = loc[0], loc[1]
            try:
                pubd = dt.date.fromisoformat(it["date"])
            except ValueError:
                pubd = today
            fin = dt.date.fromisoformat(it["delai"]) if re.match(r"\d{4}-\d\d-\d\d$", it["delai"] or "") \
                else pubd + dt.timedelta(days=DELAI_OPPOSITION_J)
            it["fin_delai"] = fin.isoformat()
            it["jours_restants"] = (fin - today).days
            it["distance"] = km(home, (it["lat"], it["lon"])) if it["lat"] is not None else None
            items.append(it)
            if done % 50 == 0:
                print(f"  … {done}/{len(ids)}")
    _save_cache()

    if not a.tous:
        items = [i for i in items if i["jours_restants"] >= 0]
    items.sort(key=lambda i: (i["distance"] if i["distance"] is not None else 1e9, i["jours_restants"]))
    proches = [i for i in items if i["distance"] is not None and i["distance"] <= a.rayon]

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(render(items, home, a, dt.datetime.now().strftime("%d.%m.%Y %H:%M")))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(proches, f, ensure_ascii=False, indent=2)
    sans = sum(1 for i in items if not i["coords_exactes"])
    print(f"✓ {len(proches)} avis à moins de {a.rayon:g} km ({len(items)} dans le canton, "
          f"{sans} placés au centre de la commune) → {os.path.abspath(a.out)}")


if __name__ == "__main__":
    main()
