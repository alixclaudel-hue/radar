# Triage des retours utilisateurs — 28/09/2026

Source : fil continu [issue #62](https://github.com/alixclaudel-hue/radar/issues/62) (commentaires automatiques « **Nouveau retour** »)
+ fichiers locaux `data/users/*/ui_notes.json` (3 comptes : `owner`, `9d9acf5415c9`, `e2a35e258dd5`).
Les 8 remarques transmises le 28/09 en session sont ajoutées en bas (série **R**).

**Comment filtrer** : remplis la colonne **Décision** avec `traiter` ou `abandonner`
(ou biffe la ligne). La colonne **Proposition** n'est qu'un avis par défaut — c'est la
colonne **Décision** qui fait foi. Une fois remplie, je traite les `traiter` et solde
les `abandonner` (statut « fait »/suppression côté `/feedback`).

Statut : `nouveau` = jamais soldé ; `déjà fait` = traité lors d'une passe antérieure.

## A. Retours issus du fil #62 (non soldés)

| # | ID | Date | Page | Retour | Proposition | Décision |
|---|----|------|------|--------|-------------|----------|
| F1 | nt_fda172ecbc | 28/09 06:57 | /reco-radar | Retirer de la playlist les vidéos vues au-delà de X % (ou seuil réglable) | abandonner | abandonné — supprimé (28/09) |
| F2 | nt_fb6e0c61e2 | 28/09 07:02 | /reco-radar | Bouton « ouvrir sur Discogs » (prix, autres tracks, déjà en wantlist/collection) | traiter | traité (28/09) |
| F3 | nt_b5dc7c0c68 | 28/09 07:19 | /reco-radar | Bug : parfois impossible de cliquer une ligne pour lire la track associée | traiter | traité (28/09) |
| F4 | nt_7a26259eeb | 28/09 07:20 | /reco-radar | Bug : la track jouée n'est plus la première de la liste (track « random » invisible) | traiter | traité (28/09) |
| F5 | nt_9af61780ff | 28/09 07:34 | /reco-radar | Ajouter Beatport (track dispo sur Beatport, pas sur Bandcamp) — voir **R3** |abandonner | abandonné — supprimé (28/09) |
| F6 | nt_c9a0124159 | 28/09 08:12 | /reco-radar | Bouton « réalimenter la playlist » + accès/visibilité de la playlist YouTube | abandonner | abandonné — supprimé (28/09) |
| F7 | nt_2acdc9d31c | 28/09 13:26 | /reco-radar | Bug : supprimer la track en cours la retire de la liste mais elle se remet à jouer | traiter | traité (28/09) |
| F8 | nt_d9c17a2ff3 | 28/09 13:33 | /reco-radar | « ç » (frappe accidentelle, aucun contenu) | abandonner | abandonné — supprimé (28/09) |
| F9 | nt_28917599fd | 28/09 13:33 | /reco-radar | Bouton pour ajouter une track à une playlist YouTube | traiter | traité (28/09) |
| F10 | nt_41ea001c56 | 28/09 14:23 | /reco-radar | Fermer la mini-fenêtre après ajout à la wantlist (elle pollue l'écran) | traiter | traité (28/09) |
| F11 | nt_173256f02f | 23/09 06:49 | /reco-radar | Jamais de reco sur tracks déjà en collection / wantlist / déjà écoutées dans la reco | traiter | traité (28/09) |
| F12 | nt_5ddca9fc87 | 27/09 22:19 | /settings | Bouton « enregistrer » par groupe de score (au lieu d'un global) | traiter | traité (28/09) |
| F13 | nt_3fab9ded7d | 14/09 15:11 | /settings | Taille max de la playlist à 300 | déjà fait | abandonné — déjà absent |
| F14 | nt_b9961a8a2a | 21/09 20:30 | /patte | Message d'encouragement (« big up de bigNath ») | abandonner | abandonné — supprimé (28/09) |

## B. Nouvelles remarques (session du 28/09)

| # | Remarque | Fichier(s) pressenti(s) | Proposition | Décision |
|---|----------|-------------------------|-------------|----------|
| R1 | Sur ordinateur, n'afficher que **3 releases par ligne** dans la page recherche (trop condensé) | `static/app.css` (`.grid`) | traiter | traité (28/09) |
| R2 | Les releases ne prennent pas la même largeur que les items au-dessus ; sur téléphone (1 ligne) c'est bon | `static/app.css` (`.grid` / `.card`) | traiter | traité (28/09) |
| R3 | Utiliser les **logos Beatport et Traxsource** dans les recherches, comme pour Bandcamp | `templates/partials/_icons.html`, `tracklist.html` | traiter | déjà fait (`tracklist.html`) |
| R4 | Le bouton « fiche Discogs » est capricieux : plusieurs clics, tombe souvent sur l'accueil Discogs | `templates/partials/results.html` (+ format `rr.uri`) | traiter | traité (28/09) |
| R5 | Les barres de recherche texte ne proposent rien après quelques caractères saisis | `templates/pages/search.html` + routes `/search/labels`, `/suggest/*` | traiter | traité (28/09) |
| R6 | Le lien d'invitation doit être accessible dans l'onglet « Mon profil », **owner seulement** | `templates/base.html`, `radar_web/app.py` (`/account/invite`) | traiter | traité (28/09) |
| R7 | Régression : des poids « score album » supérieurs à 1 sont proposés — problème de calcul | `radar/learn.py` (+ affichage Réglages) | traiter | traité (28/09) |
| R8 | Renommer les 3 catégories de `/search` : **Générale / Dans mes labels / Chez un vendeur** | `templates/pages/search.html` | traiter | traité (28/09) |

## C. Détail des correctifs appliqués (28/09)

| Réf | Résumé du correctif | Fichier(s) |
|-----|---------------------|------------|
| F2 | Lien « ouvrir sur Discogs » ajouté sur chaque ligne de reco (construit depuis l'`id` numérique du release) | `templates/partials/reco_rows.html` |
| F9 | Icône YouTube par ligne (lien vers `watch?v=<video_id>`) pour ajouter la track à une playlist (sans OAuth) | `templates/partials/_icons.html`, `templates/partials/reco_rows.html` |
| F10 | La « mini-fenêtre » des pressages vinyle (`release_matches.html`) se retire d'elle-même après un ajout wantlist réussi | `templates/partials/release_matches.html` |
| F11 | Exclusions des recos étendues à la **wantlist locale** (`cart.json`), par `release_id` **et** par identité `(artiste, titre)` — collection et déjà-écoutés l'étaient déjà | `radar_jobs/common.py` (`CART_PATH`), `radar_jobs/recos.py`, `tests/test_job_scan_recos_owned.py` |
| F12 | Bouton « 💾 Enregistrer » par groupe de score (macro `save_btn()`), en plus du global supprimé | `templates/pages/settings.html` |
| R1 | 3 releases par ligne sur ordinateur (`@media (min-width:820px)` → `repeat(3,1fr)`) | `static/app.css` |
| R2 | Cartes larges comme les items au-dessus : `auto-fill` → `auto-fit` dans `.grid` | `static/app.css` |
| R3 | Logos Beatport / Traxsource dans les tracklists (déjà en place) | `templates/partials/tracklist.html` |
| R4 | Lien « fiche Discogs » construit depuis l'`id` numérique (fini les retours à l'accueil) | `templates/partials/results.html` |
| R5 | Suggestions pour la barre « Chez un vendeur » (nouvelle route `/suggest/sellers` + combobox) | `radar_web/app.py`, `templates/pages/search.html` |
| R6 | Lien d'invitation visible dans « Mon profil » pour l'owner uniquement | `templates/base.html` |
| R7 | Poids de régression bornés à `[0, 1]` (plus de « score album » > 1) | `radar/learn.py` |
| R8 | Catégories `/search` renommées : Générale / Dans mes labels / Chez un vendeur | `templates/pages/search.html` |
