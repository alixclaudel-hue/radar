# Reco Radar — modes de playlist (Découverte / Approfondir)

Proposition rédigée le 30/09, sans code touché. Basée sur la cartographie de `radar_jobs/recos.py` (scan l.145, publish l.312), `scoring.py` (`album_score` l.856, DAG l.157) et `app.py:433` (`/reco-radar`). Les points marqués **[à vérifier]** n'ont pas été lus dans le code.

## 1. Constat sur le modèle actuel

- Un seul score par piste : `album_score` = 0,4 label + 0,4 artiste + 0,2 style, rétréci vers 45. Le terme artiste est `0,6·max + 0,4·moyenne` des `ascore` des crédités.
- `ascore` intègre déjà `graph_rescore` (voisins par co-crédits). Un artiste inconnu proche d'un aimé peut donc déjà sortir, mais **rien ne le distingue** d'un artiste connu : la playlist actuelle mélange les deux sans le dire, et le tri (score DESC) favorise structurellement le connu (score direct > score par ricochet).
- `job_scan_recos` ne fait qu'un `SELECT … WHERE score >= 60 ORDER BY score DESC` sur `track_scores`. Pas de diversité (un artiste ou un label peut occuper dix places), pas de notion de nouveauté, pas d'explication.
- Une seule playlist par compte (`recos_playlist.json`) ; historique `recos_history.json` partagé et jamais purgé (bon : à garder commun aux modes).

Conclusion : les deux modes demandés ne sont pas deux filtres sur le même score, ce sont **deux axes différents**. Il faut séparer *affinité* (« j'aime ça ») de *nouveauté* (« je ne connais pas »).

## 2. Idée centrale : deux axes au lieu d'un score

Pour chaque piste candidate, calculer et stocker (dans `track_scores.detail_json`, déjà existant) :

| Champ | Sens | Source |
|---|---|---|
| `affinity` | note actuelle (inchangée) | `album_score` |
| `artist_known` | artiste dans corpus/collection/tiers Cœur-Aimé (via `canon_artist_key`) | `artist_tier_map`, collection |
| `label_known` | label dans `label_tier_map` ou possédé | `label_tier_map` |
| `proximity_artist` | proximité à un artiste Cœur/Aimé **sans y être** | `graph_rescore` seul, hors score direct |
| `proximity_label` | proximité à un label Cœur/Aimé | `catalog_labelgraph.neighbors()` / `label_db_signal` |
| `why` | 1 à 2 ancres lisibles : « proche de X », « label voisin de Y » | argmax des liens du graphe |

Les modes deviennent des **fonctions de sélection** sur ces champs, pas des jobs distincts. Le scan calcule une fois, chaque mode pioche.

## 3. Mode Découverte

**Définition** : artiste absent du corpus ET (proche d'un artiste aimé OU sortie sur un label voisin d'un label aimé).

Éligibilité (filtres durs) :
1. `artist_known = false` pour tous les crédités principaux.
2. `proximity_artist >= s_a` **ou** `proximity_label >= s_l` (seuils à calibrer, cf. §6).
3. Pas possédé, pas dans l'historique (déjà en place).
4. Style dans le top des affinités de style (évite « proche par le graphe » mais hors genre : co-crédits ≠ goût).

Score de découverte (proposition, à valider sur données réelles) :

```
disco = 0.45·max(proximity_artist, proximity_label)
      + 0.25·affinité_style
      + 0.20·convergence      # bonus si artiste ET label sont voisins (deux signaux indépendants)
      + 0.10·fraîcheur/rareté # sortie récente ou peu de copies, léger
```

Diversité obligatoire (c'est ce qui fait qu'une playlist découverte est agréable) :
- max 1 à 2 pistes par artiste, max 3 par label dans une fournée de 20 ;
- répartition des ancres : pas plus de 30 % de pistes s'appuyant sur le même artiste aimé ;
- quota « pari » : ~15 % de pistes à proximité moyenne pour ne pas s'enfermer.

Explication affichée sur la carte : « Proche de *X* · label voisin de *Y* » (vocabulaire « Catégorie 1/2 » si on cite le tier, jamais Cœur/Aimé à l'écran).

Cas particulier à traiter : un artiste connu sous un alias ou absent de la collection mais présent dans le corpus. « Connu » doit se définir par `canon_artist_key` sur l'union corpus + collection + wantlist + achats Bandcamp, sinon la découverte propose des artistes déjà possédés.

## 4. Mode Approfondir (l'actuel) — options d'évolution

Le mode actuel est cohérent avec « approfondir » mais le tri unique par note est pauvre. Options, de la moins à la plus intrusive :

- **A. Garder le modèle, ajouter la diversité et l'explication.** Coût minimal : MMR (max 2 pistes/artiste, 3/label par fournée). Résout la monotonie, 0 risque sur le score.
- **B. Ajouter un axe « profondeur ».** Pour un artiste Cœur/Aimé, prioriser ses pistes/sorties **non encore possédées** avec un bonus au « trou dans la discographie » (sorties de l'artiste ou du label que l'utilisateur n'a pas). Répond littéralement à « approfondir son corpus ».
- **C. Modèle par tier.** Aujourd'hui `max` de l'artiste écrase la nuance. Proposer un sous-mode « artistes Catégorie 1 d'abord » vs « élargir Catégorie 2 ». Se branche sur `artist_tier_map`.
- **D. Boucle de retour.** Les pouces (👍/👎 de `reco_rows.html`, stockage **[à vérifier]**) servent à recalibrer les poids label/artiste/style par compte (régression logistique très simple sur les retours). Plus lourd, à ne faire qu'avec assez de retours (> ~100).
- **E. Refonte : score = affinité × (1 − redondance)**, où la redondance est la similarité aux pistes déjà dans la playlist. C'est le changement de modèle le plus net, mais il change les classements existants (cf. pt 36 : non additif).

Recommandation : **A + B maintenant, C ensuite, D/E après mesure.** A et B ne touchent pas la formule `album_score`, donc ne reclassent pas les recos de façon imprévisible.

## 5. Architecture technique

- **Données** : `recos_playlist.json` → `recos_playlist_<mode>.json` (`decouverte`, `approfondir`). Migration : le fichier actuel devient `approfondir` (renommage au premier chargement, sans perte). Candidats idem (`recos_candidates_<mode>.json`). `recos_history.json` reste **commun** : une piste vue dans un mode ne revient pas dans l'autre.
- **Scan** (`job_scan_recos`) : un seul passage sur `track_scores`, classe chaque piste par mode via une fonction pure `select_for_mode(row, mode, ctx)` (testable hors ligne). Ajouter `artist_known`, `proximity_*`, `why` au `detail_json` dans `job_scorestore_tracks` **[à vérifier : coût de recalcul ; prévoir un rattrapage incrémental]**.
- **Publish** : boucle par mode, `max_tracks` par mode. **Budget YouTube (80/jour, partagé, pt 31)** : deux playlists se partagent le même quota. Proposition : 50/50 avec report du reliquat, et dans les deux cas Discogs-vidéo d'abord (gratuit). Découverte pénalisée car artistes peu connus = moins de vidéos Discogs → surveiller le taux de match (pt 32).
- **Web** : `/reco-radar?mode=decouverte|approfondir`, sélecteur en deux liens-onglets (zéro JS, comme le menu profil). Défaut `approfondir` pour ne pas casser les usages existants. Chaque mode garde sa purge à minuit et son état « écoutée ».
- **Multi-compte** : `paths.user_file(uid, …)` pour les nouveaux fichiers, le worker itère déjà sur `all_uids()` (pt 19).
- **CI (piège pt 6)** : la route garde le même chemin, le paramètre `mode` a une valeur par défaut, donc les listes de routes de `ci.yml` ne changent pas. À revérifier tout de même.

## 6. Calibrage et validation (avant de coder les seuils)

1. Script hors ligne (lecture de `scorestore`, aucun réseau) qui, pour chaque mode, sort les 30 premières pistes et les distributions de `proximity_*` : voir si la découverte a assez de volume (combien de pistes passent `artist_known=false` + seuils ?). Risque principal : trop peu de candidats si le graphe local est étroit.
2. Rejeu sur l'historique : parmi les pistes déjà 👍, quelle proportion aurait été classée « découverte » (artiste inconnu à l'époque) ? Donne une mesure de précision de la proximité.
3. Banc du pt 39 : ajouter des cas qui échouent avant correctif (artiste possédé qui passe en découverte ; 10 pistes du même label dans une fournée).

## 7. Découpage en PR

| PR | Contenu | Risque |
|---|---|---|
| 1 | Diversité + `why` sur le mode actuel (A) | faible |
| 2 | Champs `artist_known`/`proximity_*` dans `track_scores` + script de calibrage | moyen (recalcul scorestore) |
| 3 | Fichiers par mode + migration + `select_for_mode` + budget partagé | moyen |
| 4 | UI onglets + explication sur carte | faible, **revue navigateur nécessaire** (travail visuel, non délégable) |
| 5 | Approfondir B (profondeur) puis C | moyen |
| 6 | Boucle de retour D | après mesure |

Tests à écrire : `select_for_mode` (connu/inconnu, alias), diversité, migration du fichier, budget partagé, route avec/sans `mode`, isolation par uid. Suites existantes touchées : `test_job_scan_recos_owned.py`, `test_job_publish_recos.py`, `test_worker_recos_multi_uid.py`.

## 8. Décisions (utilisateur, 30/09)

1. **Bascule entre les modes** : une seule playlist visible à la fois, onglets.
2. **Découverte : proximité artiste privilégiée** (pondération du score : `proximity_artist` domine, `proximity_label` en appoint ; à traduire dans la formule §3).
3. **Quota YouTube 50/50** entre les deux modes, report du reliquat.
4. **Approfondir : A + B** (diversité/explication + axe profondeur), formule `album_score` inchangée.
5. **La wantlist compte comme « connu ».**

## 9. Limites de cette proposition

Les poids et seuils sont des hypothèses, non calibrés sur tes données. Je n'ai pas lu le code des pouces ni le calcul de `proximity` dans `graph_rescore` : leur extraction séparée du score direct est supposée faisable. Rien n'a été exécuté ni vérifié dans un navigateur.
