# Mises à l'enquête en Valais

Page web qui liste les **mises à l'enquête publique** publiées au **Bulletin officiel du Valais** autour d'une commune, avec carte, distance, délai d'opposition et lien vers l'avis officiel.

- Sources : API publique du Portail des feuilles officielles (amtsblattportal.ch, SECO), sous-rubriques
  **BA-VS05** (demandes d'autorisation de construire) et **BA-VS10** (mises en consultation publique des plans) ;
  géocodage swisstopo (api3.geo.admin.ch).
- Pour chaque avis : projet, commune, parcelle, zone, dérogations, requérant, n° eConstruction, **délai indiqué dans l'avis**,
  position **exacte** (coordonnées de l'avis, sinon centre du village).
- **Faire opposition** : sur chaque avis encore ouvert, un formulaire prépare une lettre d'opposition (ou de réserve de droit privé) pré-remplie avec les références de l'avis et l'adresse de l'autorité indiquée dans la publication, à imprimer ou enregistrer en PDF, signer et envoyer avant le délai. Rien n'est envoyé : tout se passe dans le navigateur.
- Dans la page : point de référence par **commune ou NPA** (suggestions au fil de la saisie, clavier ↑ ↓ Entrée Échap) ou par **position GPS** (bouton ⌖), rayon (1–60 km), recherche, type d'avis, avis échus. Les réglages sont mémorisés dans le navigateur.

## Utilisation en local

Python 3.8+ seul, sans dépendance :

```sh
python3 scripts/enquetes_vs.py                          # Evionnaz, 15 km, 30 jours → enquetes.html
python3 scripts/enquetes_vs.py --home Martigny --rayon 8
python3 scripts/enquetes_vs.py --jours 60 --tous        # y c. délais échus
python3 scripts/enquetes_vs.py --json enquetes.json     # export des avis dans le rayon
python3 scripts/enquetes_vs.py --debug                  # XML du 1er avis + champs extraits
```

## En ligne (GitHub Actions + Netlify)

L'API du Bulletin officiel ne répond pas aux serveurs de build Netlify (connexion expirée) ; elle répond en revanche aux serveurs de GitHub. Donc :

1. Le workflow `.github/workflows/publier.yml` génère la page **chaque matin** (et à chaque modification du script sur `master`) et versionne `site/index.html` et `site/enquetes.json` s'il y a du nouveau.
2. Netlify publie le dossier `site/` tel quel, sans étape de build (`netlify.toml`).

Pour forcer une mise à jour : onglet *Actions* → *Publier les mises à l'enquête* → *Run workflow*.
Sur une pull request, le workflow vérifie seulement que la génération fonctionne.

> Le lien de chaque avis ouvre la publication officielle, seule à faire foi.
