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
.ctl label{display:grid;gap:4px;font-size:13px;color:var(--mut);font-weight:600}
input,select{font:16px system-ui;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
#home{width:220px}#q{width:min(340px,100%)}
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
@media (max-width:560px){.ctl{display:grid;grid-template-columns:1fr 1fr}.ctl label:nth-child(3),.ctl #err{grid-column:1/-1}#home,#q,input[type=range]{width:100%}}
</style></head><body>
<header>
 <h1 id="h1">Mises à l'enquête</h1>
 <div class="sub" id="sub"></div>
 <div class="ctl">
  <label>Autour de<input id="home" list="hl" autocomplete="off" placeholder="Commune ou NPA"></label>
  <label><span>Rayon : <span id="rv"></span> km</span><input type="range" id="r" min="1" max="60" step="1"></label>
  <label>Rechercher<input id="q" type="search" placeholder="Commune, mot-clé, parcelle, requérant…"></label>
  <label>Type<select id="t"><option value="">Tous</option><option>Autorisation de construire</option><option>Consultation des plans</option></select></label>
  <label class="chk"><input type="checkbox" id="late"> Inclure les délais échus</label>
  <span id="err" role="status"></span>
 </div>
</header>
<div class="layout">
 <div id="map"></div>
 <main><section id="list"></section>
 <p class="foot">Source : Bulletin officiel du Valais (amtsblattportal.ch), généré le __GENERATED__ (publications des __JOURS__ derniers jours).
 Le lien de chaque avis ouvre la publication officielle, seule à faire foi. Le délai affiché est celui indiqué dans l'avis
 (à défaut : __DELAI__ jours dès la publication). Les dossiers se consultent sur eConstruction avec le numéro indiqué, ou auprès de la commune.</p></main>
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
const me=L.circleMarker([home.lat,home.lon],{radius:7,color:'#14181d',weight:2,fillColor:'#fff',fillOpacity:1}).addTo(map).bindTooltip('Référence');
const layer=L.layerGroup().addTo(map);const markers={};

function el(t,c,x){const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=x;return n;}
function card(d){
 const a=el('article','c');a.dataset.id=d.id;
 const h=el('div','h');h.append(el('span','d',d.dist.toFixed(1)+' km'),el('span','com',d.commune),el('span','tp',d.type));
 const b=el('span','b '+(d.left<0?'late':d.left<=7?'hot':''),d.left<0?'échu':d.left===0?'dernier jour':'J-'+d.left);h.append(b);a.append(h);
 const h3=el('h3');const l=el('a',null,d.projet||d.titre||'Avis');l.href=d.url;l.target='_blank';l.rel='noopener';h3.append(l);a.append(h3);
 const dl=el('dl');[['Parcelle','parcelle'],['Zone','zone'],['Dérogations','derogations'],['Requérant','requerant'],['eConstruction','econstruction']]
  .forEach(([k,v])=>{if(d[v]){dl.append(el('dt',null,k),el('dd',null,d[v]));}});if(dl.children.length)a.append(dl);
 const end=pd(d.fin_delai);
 a.append(el('small',null,'Publié le '+fmt.format(pd(d.date))+' · oppositions jusqu’au '+(end?fmtY.format(end):'?')+(d.coords_exactes?'':' · position approximative (centre de la commune)')));
 a.addEventListener('click',e=>{if(e.target.closest('a'))return;const m=markers[d.id];if(m){map.setView(m.getLatLng(),Math.max(map.getZoom(),15));m.openPopup();}select([d.id]);});
 return a;
}
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
  m.bindPopup(p);m.on('click',()=>{select([d.id]);const c=document.querySelector('.c[data-id="'+d.id+'"]');if(c)c.scrollIntoView({behavior:'smooth',block:'center'});});
  m.addTo(layer);markers[d.id]=m;});
 if(fit)map.fitBounds(circle.getBounds(),{padding:[10,10]});
 try{localStorage.setItem('enq-home',JSON.stringify(home));localStorage.setItem('enq-r',radius);}catch(e){}
}
DATA.forEach(d=>d.search=[d.commune,d.projet,d.titre,d.parcelle,d.requerant,d.zone,d.econstruction,d.numero].join(' ').toLowerCase());

$('#r').addEventListener('input',e=>{radius=+e.target.value;update(false);});
$('#r').addEventListener('change',()=>update(true));
['#q','#t','#late'].forEach(s=>$(s).addEventListener('input',()=>update(false)));
const dlist=document.createElement('datalist');dlist.id='hl';document.body.append(dlist);
let sugg=[],tm;
$('#home').addEventListener('input',e=>{clearTimeout(tm);const v=e.target.value.trim();
 const hit=sugg.find(s=>s.label===v);if(hit){choose(hit);return;}
 if(v.length<2)return;tm=setTimeout(()=>geo(v),250);});
$('#home').addEventListener('change',e=>{const v=e.target.value.trim();if(v===home.label)return;
 const hit=sugg.find(s=>s.label===v)||sugg[0];if(hit)choose(hit);else if(v)geo(v,true);});
const GEO='https://api3.geo.admin.ch/rest/services/api/SearchServer?type=locations&sr=4326&';
function geo(v,apply){
 fetch(GEO+'origins=gg25,zipcode&limit=6&searchText='+encodeURIComponent(v))
 .then(r=>r.json()).then(j=>{sugg=(j.results||[]).filter(r=>r.attrs&&r.attrs.lat).map(r=>({lat:r.attrs.lat,lon:r.attrs.lon,label:r.attrs.label.replace(/<[^>]+>/g,'').trim()}));
  dlist.textContent='';sugg.forEach(s=>{const o=document.createElement('option');o.value=s.label;dlist.append(o);});
  if(apply){if(sugg[0])choose(sugg[0]);else $('#err').textContent='Commune introuvable.';}})
 .catch(()=>{if(apply)$('#err').textContent='Recherche indisponible.';});
}
// Centre du village (« Ort » swisstopo) plutôt que centre géométrique de la commune
function choose(s){
 $('#err').textContent='';home=s;update(true);
 const name=s.label.replace(/^\d{4}\s*-\s*|\s*\([A-Z]{2}\)$/g,'').trim();
 fetch(GEO+'origins=gazetteer&limit=15&searchText='+encodeURIComponent(name)).then(r=>r.json()).then(j=>{
  const esc=name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const hit=(j.results||[]).find(r=>r.attrs&&r.attrs.label.includes('<i>Ort</i>')&&new RegExp('<b>\\s*'+esc+'\\s*</b>','i').test(r.attrs.label));
  if(hit&&home===s){home={lat:hit.attrs.lat,lon:hit.attrs.lon,label:s.label};update(true);}
 }).catch(()=>{});
}
update(true);
})();
</script></body></html>"""


def render(items, home, args, generated):
    data = [{k: it.get(k) for k in ("id", "numero", "type", "titre", "projet", "date", "fin_delai", "commune",
                                     "parcelle", "zone", "derogations", "requerant", "econstruction",
                                     "coords_exactes", "lat", "lon", "url")}
            for it in items if it.get("lat") is not None]
    cfg = {"home": list(home), "rayon": int(round(args.rayon)) or 15, "jours": args.jours,
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
