#!/usr/bin/env python3
"""
maj_communes.py — Coordonnées des administrations communales (VS, VD, GE).

Construit data/communes_vs.json (clé : numéro OFS de la commune, toutes cantons
confondus malgré le nom du fichier) à partir de :
  - Wikidata : nom et site officiel ;
  - OpenStreetMap (Overpass) : bâtiment de l'administration communale
    (amenity=townhall) : adresse, téléphone, horaires, e-mail ;
  - page d'accueil du site communal : numéro de téléphone (souvent en pied de page).

Les corrections faites à la main vont dans data/communes_vs_manuel.json
(même format, champs facultatifs) : elles priment toujours sur ce fichier.

Usage : python3 scripts/maj_communes.py [--sans-sites]
"""
import argparse, datetime as dt, html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "communes_vs.json")
UA = "enquetes-vs/1.1 (usage personnel)"
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]

SPARQL = """
SELECT ?bfs ?label ?web WHERE {
  VALUES ?k { wd:Q834 wd:Q12771 wd:Q11917 }
  ?c wdt:P31 wd:Q70208; wdt:P131+ ?k.
  FILTER NOT EXISTS { ?c wdt:P576 ?end }
  ?c wdt:P771 ?bfs.
  OPTIONAL { ?c wdt:P856 ?web }
  ?c rdfs:label ?label FILTER(lang(?label)="fr")
}"""

OVERPASS_Q = """[out:json][timeout:300];
area["ISO3166-2"="__KT__"]["admin_level"="4"]->.vs;
rel(area.vs)["boundary"="administrative"]["admin_level"="8"];
map_to_area->.communes;
foreach.communes->.c(
  .c out tags;
  nwr(area.c)["amenity"="townhall"];
  out center tags;
);"""


def fetch(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def wikidata(path=None):
    if path:  # résultat SPARQL déjà téléchargé (JSON)
        raw = open(path, "rb").read()
        return parse_wd(raw)
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": SPARQL})
    for i in range(4):
        try:
            raw = fetch(url, headers={"Accept": "application/sparql-results+json"}, timeout=90)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 3:
                raise
            print("  Wikidata limite les requêtes, nouvel essai dans 65 s", file=sys.stderr)
            time.sleep(65)
    return parse_wd(raw)


def parse_wd(raw):
    out = {}
    for b in json.loads(raw)["results"]["bindings"]:
        bfs = b["bfs"]["value"]
        x = out.setdefault(bfs, {"nom": b["label"]["value"]})
        if "web" in b and "site" not in x:
            x["site"] = b["web"]["value"]
    return out


def overpass(path=None, kt="CH-VS"):
    data = urllib.parse.urlencode({"data": OVERPASS_Q.replace("__KT__", kt)}).encode()
    for url in ([] if path else OVERPASS):
        try:
            d = json.loads(fetch(url, data=data, timeout=200))
            break
        except Exception as e:
            print(f"  Overpass {url} : {e}", file=sys.stderr)
    else:
        if not path:
            return {}
        d = json.load(open(path, encoding="utf-8"))  # résultat Overpass déjà téléchargé
    out, cur = {}, None
    for e in d.get("elements", []):
        t = e.get("tags", {})
        if e["type"] == "area":
            cur = t.get("swisstopo:BFS_NUMMER")
            continue
        if cur:
            out.setdefault(cur, []).append(t)
    best = {}
    for bfs, ts in out.items():
        ts.sort(key=lambda t: (t.get("townhall:type") not in (None, "municipal"), -len(t)))
        t = ts[0]
        g = lambda *ks: next((t[k] for k in ks if t.get(k)), "")
        street = " ".join(x for x in (g("addr:street"), g("addr:housenumber")) if x)
        city = " ".join(x for x in (g("addr:postcode"), g("addr:city")) if x)
        best[bfs] = {k: v for k, v in {
            "adresse": ", ".join(x for x in (street, city) if x),
            "telephone": g("phone", "contact:phone"),
            "horaires_osm": g("opening_hours"),
            "email": g("email", "contact:email"),
            "site": g("website", "contact:website"),
        }.items() if v}
    return best


# indicatifs romands : 021 (Lausanne), 022 (Genève), 024, 026, 027, 032, et 058 (administrations)
TEL = re.compile(r"(?:\+41[\s.]?|0)(?:2[1-7]|32|58)[\s.]?\d{3}[\s.]?\d{2}[\s.]?\d{2}")


def phone_from_site(url):
    try:
        raw = fetch(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25).decode("utf-8", "replace")
    except Exception:
        return ""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    tels = [m for m in re.findall(r'href="tel:([^"]+)"', t)] + TEL.findall(html.unescape(re.sub(r"<[^>]+>", " ", t)))
    for x in tels:
        d = re.sub(r"[^\d+]", "", x)
        if d.startswith("+41"):
            d = "0" + d[3:]
        if re.fullmatch(r"0(2[1-7]|32|58)\d{7}", d):
            return f"{d[:3]} {d[3:6]} {d[6:8]} {d[8:]}"
    return ""


def fmt_tel(t):
    d = re.sub(r"[^\d+]", "", t or "")
    if d.startswith("+41"):
        d = "0" + d[3:]
    return f"{d[:3]} {d[3:6]} {d[6:8]} {d[8:]}" if re.fullmatch(r"0\d{9}", d) else (t or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sans-sites", action="store_true", help="ne pas lire les sites communaux")
    ap.add_argument("--osm", help="fichier JSON d'un résultat Overpass déjà téléchargé (premier canton)")
    ap.add_argument("--cantons", default="VS,VD,GE", help="cantons (défaut : VS,VD,GE)")
    ap.add_argument("--wikidata", help="fichier JSON d'un résultat SPARQL Wikidata déjà téléchargé")
    a = ap.parse_args()

    print("→ Wikidata"); wd = wikidata(a.wikidata); print(f"  {len(wd)} communes")
    osm = {}
    for i, kt in enumerate(a.cantons.split(",")):
        print(f"→ OpenStreetMap {kt}")
        got = overpass(a.osm if (a.osm and i == 0) else None, "CH-" + kt.strip())
        print(f"  {len(got)} administrations communales")
        osm.update(got)
    sites = {}
    if not a.sans_sites:
        todo = [(b, x["site"]) for b, x in wd.items() if x.get("site")]
        print(f"→ Sites communaux ({len(todo)})")
        with ThreadPoolExecutor(8) as ex:
            for (b, _), tel in zip(todo, ex.map(lambda bt: phone_from_site(bt[1]), todo)):
                if tel:
                    sites[b] = tel
        print(f"  {len(sites)} numéros trouvés")

    today = dt.date.today().isoformat()
    try:  # valeurs déjà connues : conservées si une source ne répond pas cette fois-ci
        with open(OUT, encoding="utf-8") as f:
            before = json.load(f)
    except (OSError, ValueError):
        before = {}
    out = {}
    for bfs, w in sorted(wd.items(), key=lambda kv: kv[1]["nom"]):
        o = osm.get(bfs, {})
        rec = {"nom": w["nom"], "site": w.get("site") or o.get("site", "")}
        if o.get("adresse"):
            rec["adresse"] = o["adresse"]
        tel = fmt_tel(o.get("telephone")) or sites.get(bfs, "")
        if tel:
            rec["telephone"] = tel
            rec["source_telephone"] = "OpenStreetMap" if o.get("telephone") else "site communal"
        if o.get("horaires_osm"):
            rec["horaires_osm"] = o["horaires_osm"]
        if o.get("email"):
            rec["email"] = o["email"]
        old = before.get(bfs, {})
        for k in ("adresse", "telephone", "source_telephone", "horaires_osm", "email"):
            if not rec.get(k) and old.get(k):
                rec[k] = old[k]
        rec["maj"] = today
        out[bfs] = rec
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    n = lambda k: sum(1 for r in out.values() if r.get(k))
    print(f"✓ {len(out)} communes → {OUT} (site {n('site')}, téléphone {n('telephone')}, "
          f"adresse {n('adresse')}, horaires {n('horaires_osm')})")


if __name__ == "__main__":
    main()
