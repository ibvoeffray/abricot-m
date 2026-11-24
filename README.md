# Mises à l'enquête en Valais

Page web qui liste les **mises à l'enquête publique** publiées au **Bulletin officiel du Valais** autour d'une commune, avec carte, distance, délai d'opposition et lien vers l'avis officiel.

- Sources : API publique du Portail des feuilles officielles (amtsblattportal.ch, SECO), sous-rubriques
  **BA-VS05** (demandes d'autorisation de construire) et **BA-VS10** (mises en consultation publique des plans) ;
  géocodage swisstopo (api3.geo.admin.ch).
- Pour chaque avis : projet, commune, parcelle, zone, dérogations, requérant, n° eConstruction, **délai indiqué dans l'avis**,
  position **exacte** (coordonnées de l'avis, sinon centre du village).
- Dans la page : choix de la commune de référence, rayon (1–60 km), recherche, type d'avis, avis échus. Les réglages sont mémorisés dans le navigateur.

## Utilisation en local

Python 3.8+ seul, sans dépendance :

```sh
python3 scripts/enquetes_vs.py                          # Evionnaz, 15 km, 30 jours → enquetes.html
python3 scripts/enquetes_vs.py --home Martigny --rayon 8
python3 scripts/enquetes_vs.py --jours 60 --tous        # y c. délais échus
python3 scripts/enquetes_vs.py --json enquetes.json     # export des avis dans le rayon
python3 scripts/enquetes_vs.py --debug                  # XML du 1er avis + champs extraits
```

## En ligne (Netlify)

`netlify.toml` lance le script à chaque déploiement et publie `site/` (la page n'est pas versionnée).
Pour une mise à jour quotidienne automatique :

1. Netlify → *Site configuration → Build & deploy → Build hooks* : créer un hook.
2. GitHub → *Settings → Secrets and variables → Actions* : ajouter le secret `NETLIFY_BUILD_HOOK` avec l'URL du hook.

Le workflow `.github/workflows/rafraichir.yml` déclenche alors un déploiement chaque matin (et à la demande depuis l'onglet *Actions*).

> Le lien de chaque avis ouvre la publication officielle, seule à faire foi.
