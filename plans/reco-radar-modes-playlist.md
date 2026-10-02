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

> **Révisé le 30/09 (décisions 6 à 8, §8)** : la découverte se définit par la **distance dans les graphes** (1 ou 2 sauts) depuis ce qui est connu, pas par « artiste inconnu ». Le texte d'origine ci-dessous reste valable pour le score et la diversité, pas pour l'éligibilité.

**Définition** : une piste est « découverte » si **au moins une** des deux voies la retient.

- **Voie artiste** : artiste **ni en Catégorie 1/2, ni dans le corpus** (ni collection/wantlist/achats Bandcamp), situé à **1 ou 2 sauts** d'un artiste connu dans le graphe de co-crédits (`job_build_graph`, `radar_jobs/graph.py`). 1 saut = déjà calculé (`Ctx.graph_rescore()["artists"]`, avec `why` « N× avec X ») ; 2 sauts = à construire.
- **Voie label** : label **pas en Catégorie 1/2**, situé à **1 ou 2 sauts** d'un label Catégorie 1/2 dans le graphe label↔label (`catalog_labelgraph.neighbors_for`, artistes partagés et sous-labels). 1 saut = déjà agrégé dans `Ctx._compute_label_db_signal` ; 2 sauts = second passage de `neighbors_for` sur les voisins directs.
- Un artiste connu sur un label voisin passe par la voie label. Tout ce qui n'est retenu par aucune voie reste en Approfondir (partition par piste, jamais les deux).

**Lien fort au 2ᵉ saut (décision 8)** : un voisin de voisin n'est retenu que s'il est relié par **≥ 2 co-crédits** ou par **≥ 2 chemins distincts** depuis les graines — sinon un artiste croisé une fois sur une compilation remonterait. Expansion bornée (nb d'artistes et de co-crédits développés par voisin), calculée dans le passage mensuel de `build_graph`, jamais dans le scan horaire.

Pondération : 1 saut > 2 sauts ; graine Catégorie 1 > Catégorie 2 (`scoring.graph.tier_w`) ; voie artiste > voie label (décision 2) ; bonus de convergence si les deux voies retiennent la piste.

Éligibilité (filtres durs, en plus) :
1. Pas possédé, pas dans l'historique (déjà en place).
2. Style dans le top des affinités de style (évite « proche par le graphe » mais hors genre : co-crédits ≠ goût).

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
6. **Découverte = proximité dans les graphes, pas « artiste inconnu »** : voie label (voisins des labels Catégorie 1/2) et voie artiste (artistes hors Catégorie 1/2 et hors corpus, voisins des artistes connus).
7. **Portée : 1 ou 2 sauts** dans les deux graphes.
8. **Lien fort exigé au 2ᵉ saut** (≥ 2 co-crédits ou ≥ 2 chemins), expansion bornée.

Prochaine étape (§6.1, avant tout code de sélection) : script de mesure hors ligne — volume de pistes « découverte » dans `track_scores` à 1 saut puis à 2 sauts, top 30, et part des voisins dont les sorties ne sont jamais notées par scorestore (à élargir sinon).

## 9. Limites de cette proposition

Les poids et seuils sont des hypothèses, non calibrés sur tes données. Je n'ai pas lu le code des pouces ni le calcul de `proximity` dans `graph_rescore` : leur extraction séparée du score direct est supposée faisable. Rien n'a été exécuté ni vérifié dans un navigateur.

## 10. Mesures du 01/10 et décisions 9-10

Mesures (compte owner, `scripts/reco_modes_measure.py` puis `scripts/reco_modes_widths.py`) : sans filtre, le 1er saut couvre déjà 460 k labels et 153 k artistes ; la voie label donne 0 piste par construction (scorestore ne note que les labels Catégorie 1/2) ; les graines labels sont polluées (5 479 labels en Catégorie 1 dont « Not On Label », Columbia, CBS, BMG…) et le poids brut favorise les majors. Voie artiste saine : K=10 voisins par graine, ≥ 3 co-crédits → ~550 artistes.

9. **Découverte = voie artiste seule** (K=10, n ≥ 3). La voie label (et le 2ᵉ saut) est **abandonnée**. Le tri des 5 500 labels Catégorie 1 est un chantier séparé, plus tard.
10. **Pour chaque utilisateur** (boucle worker `all_uids()`, comme le mode actuel).

## 11. Conception retenue (étape 1)

- `radar_web/radar/discovery.py` : `discovery_artists(ctx, k=10, min_n=3)` — inverse `ctx.graph["edges"]` (artiste voisin → graines `co` avec `n`), garde par graine de `artist_tier_map()` ses K voisins de plus fort `n` (≥ min_n), exclut les artistes connus (Catégorie 1/2, corpus, collection). Score et `why` repris de `ctx.graph_rescore()["artists"]`. Fonction pure, testable hors ligne.
- `discogs_dump.releases_for_artists(artist_ids, per_artist)` : sorties du dump d'un lot d'artistes (via `release_artists`), même forme de ligne que `search_local`, tous formats.
- **Source = dump au scan, pas scorestore** : `scorestore_releases` élague tout label hors Catégorie 1/2 (`prune_labels`), il effacerait les sorties Découverte. Scan en deux temps : note des sorties (`album_score`), puis tracklists (`tracks_for_release`) des meilleures seulement.
- Tri Découverte : proximité de l'artiste dominante (décision 2) puis affinité (`album_score`). Diversité : 1 piste par artiste, 2 par label.
- Fichiers : Approfondir garde `recos_playlist.json` / `recos_candidates.json` (aucune migration) ; Découverte = `recos_playlist_decouverte.json` / `recos_candidates_decouverte.json`. Historique commun.
- Jobs : `scan_recos_decouverte`, `publish_recos_decouverte` (noms distincts : dédoublonnage de la file worker par `(uid, nom)`). Budget YouTube 80/jour partagé : 40 par mode, le reliquat d'un mode sans candidats passe à l'autre.
- Web : `/reco-radar?mode=approfondir|decouverte` (défaut approfondir), onglets, ligne « pourquoi » sur la carte ; les actions (supprimer, écoutée, vider la file, playlist YouTube) portent le mode.
