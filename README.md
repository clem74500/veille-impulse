# Veille Impulse

Scanner automatique des marchés publics vidéo / photo / motion design pour Impulse Creative Agency.

- `scanner/scan.py` interroge le BOAMP et TED, filtre (CPV audiovisuel, mots-clés, exclusions vidéosurveillance & co), score la pertinence (Rhône > AURA > France) et garde un historique sans doublons.
- `scanner/config.json` : mots-clés, exclusions, départements, codes CPV. C'est le seul fichier à modifier pour régler la veille.
- `.github/workflows/scan.yml` lance le scan chaque matin en semaine et enregistre les résultats dans `data/`.
- Chaque matin, une tâche planifiée Claude lit `data/nouvelles.json`, ajoute les alertes reçues par mail, résume chaque marché et remplit le tableau de bord.

Lancer à la main : onglet **Actions** → *Veille marchés publics* → **Run workflow**.
