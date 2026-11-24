# Mises à l'enquête – Suisse romande

Page web (en **français, allemand et italien**) qui liste les **mises à l'enquête publique** autour d'une commune, dans les **cantons romands** (VS, VD, GE, FR, NE, JU) et la **partie francophone du canton de Berne**, avec carte, distance, délai d'opposition et lien vers l'avis officiel.

| Canton | Source | Délai |
|---|---|---|
| **VS** | Bulletin officiel du Valais, API amtsblattportal.ch (SECO), BA-VS05 / BA-VS10 | indiqué dans l'avis |
| **BE** | Feuille officielle du canton de Berne, API amtsblattportal.ch, BP-BE10, avis en français | indiqué dans l'avis |
| **VD** | Couche CAMAC « Demandes de permis de construire (enquête publique) », ogc.vd.ch | fin de l'enquête |
| **GE** | SITG « Autorisation de construire – Dossier » (enregistrés ou en instruction) | non fourni : 30 jours dès la publication FAO |
| **FR** | Moteur de recherche des mises à l'enquête de l'État de Fribourg (fr.ch), commune par commune | fin de l'enquête |
| **NE** | SITN, couche « autorisations de construire – pendant l'enquête » (WFS) | fin de l'enquête |
| **JU** | Journal officiel (PDF hebdomadaire), rubrique « Avis de construction » ; nécessite `pdftotext` (poppler-utils) | date indiquée dans l'avis |

Commune : numéro OFS fourni (VS, BE, FR) ou retrouvé par les contours communaux du canton (VD, GE, NE) ou par le nom (JU). Position : coordonnées de l'avis (VS, VD, GE, NE) ou adresse géocodée par swisstopo (BE, FR, JU), à défaut centre de la commune. Seuls les avis officiels font foi.

**Langues** : interface FR / DE / IT (sélecteur en haut à droite, langue du navigateur par défaut). Le contenu des avis reste dans sa langue de publication. La lettre d'opposition est rédigée dans la langue de l'avis (allemand pour le Haut-Valais, français ailleurs), puisqu'elle s'adresse à l'autorité qui l'a publié.

- Pour chaque avis : projet, commune, parcelle, zone, dérogations, requérant, n° eConstruction, **délai indiqué dans l'avis**,
  position **exacte** (coordonnées de l'avis, sinon centre du village).
- **Faire opposition** : sur chaque avis encore ouvert, un formulaire prépare une lettre d'opposition (ou de réserve de droit privé) pré-remplie avec les références de l'avis et l'adresse de l'autorité indiquée dans la publication, à imprimer ou enregistrer en PDF, signer et envoyer avant le délai. Rien n'est envoyé : tout se passe dans le navigateur.
- Dans la page : point de référence par **commune ou NPA** (suggestions au fil de la saisie, clavier ↑ ↓ Entrée Échap) ou par **position GPS** (bouton ⌖), rayon (1–10 km, 2 km par défaut), recherche, type d'avis, avis échus. Les réglages sont mémorisés dans le navigateur.

## Coordonnées des communes

Chaque avis propose « Contacter la commune » (1027 communes des 7 cantons) : adresse, téléphone, e-mail, horaires et site de l'administration communale.

- `data/communes_vs.json` est construit par `python3 scripts/maj_communes.py` à partir de Wikidata (site officiel), d'OpenStreetMap (adresse, téléphone, horaires des administrations communales) et de la page d'accueil des sites communaux (téléphone). Relancé automatiquement le 3 de chaque mois (`.github/workflows/communes.yml`).
- L'adresse publiée par la commune dans ses propres avis prime sur celle d'OpenStreetMap.
- **Corrections à la main** : `data/communes_vs_manuel.json`, par numéro OFS de la commune ; ces valeurs priment sur tout le reste. Exemple :

```json
{
  "6213": {
    "telephone": "027 767 11 13",
    "horaires": "lu–ve 8h00–11h30, ma et je aussi 14h00–17h00",
    "email": "commune@exemple.ch"
  }
}
```

Champs possibles : `service`, `adresse`, `telephone`, `email`, `horaires` (texte libre), `site`. Les horaires ne sont connus que pour une minorité de communes ; sinon la page renvoie vers le site de la commune.

## Utilisation en local

Python 3.8+ seul, sans dépendance :

```sh
python3 scripts/enquetes_vs.py                          # Evionnaz, 2 km, 30 jours → enquetes.html
python3 scripts/enquetes_vs.py --home Martigny --rayon 8
python3 scripts/enquetes_vs.py --jours 60 --tous        # y c. délais échus
python3 scripts/enquetes_vs.py --json enquetes.json     # export des avis dans le rayon
python3 scripts/enquetes_vs.py --cantons VS,VD          # certains cantons seulement
python3 scripts/enquetes_vs.py --debug                  # XML du 1er avis VS
```

## En ligne (GitHub Actions + Netlify)

L'API du Bulletin officiel ne répond pas aux serveurs de build Netlify (connexion expirée) ; elle répond en revanche aux serveurs de GitHub. Donc :

1. Le workflow `.github/workflows/publier.yml` génère la page **chaque matin** (et à chaque modification du script sur `master`) et versionne `site/index.html` et `site/enquetes.json` s'il y a du nouveau.
2. Netlify publie le dossier `site/` tel quel, sans étape de build (`netlify.toml`).

Pour forcer une mise à jour : onglet *Actions* → *Publier les mises à l'enquête* → *Run workflow*.
Sur une pull request, le workflow vérifie seulement que la génération fonctionne.

> Le lien de chaque avis ouvre la publication officielle, seule à faire foi.
