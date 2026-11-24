# Mise à l'enquête publique en ligne

Site statique présentant un dossier mis à l'enquête publique (permis de construire, plan d'affectation…) :

- **Statut en direct** : enquête à venir / en cours (jours restants) / close, calculé à partir des dates.
- **Documents** du dossier (PDF) classés par catégorie, avec recherche.
- **Présentation du projet**, chiffres clés et **carte** de situation (OpenStreetMap).
- **Formulaire d'observation / d'opposition** (reçu via Netlify Forms) et **génération d'un courrier prêt à signer** (impression / PDF). Le formulaire se désactive automatiquement à la fin du délai.
- Procédure, questions fréquentes, contact.

## Modifier le contenu

Tout le contenu se trouve dans [`site/content/enquete.json`](site/content/enquete.json) :
textes, dates (`dateDebut`, `dateFin` au format `AAAA-MM-JJ`, la date de fin est incluse), documents, coordonnées de la carte, adresse postale, FAQ, contact.

Les PDF se placent dans `site/documents/` (les fichiers actuels sont des **documents provisoires** à remplacer).

Une interface d'édition (Decap CMS) est disponible sur `/admin/` une fois Netlify Identity et Git Gateway activés sur le site Netlify.

## Développement local

```sh
cd site && python3 -m http.server 8000
# puis ouvrir http://localhost:8000
```

## Déploiement

Netlify publie directement le dossier `site/` (voir `netlify.toml`), sans étape de compilation. Les remarques envoyées apparaissent dans l'onglet **Forms** de Netlify (formulaire `remarques`), où l'on peut configurer une notification par e-mail vers le service communal.
