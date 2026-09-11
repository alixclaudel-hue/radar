# Radar — instructions projet

Outil perso crate-digging vinyle basé sur Discogs : ingère écoute (YouTube, Spotify, Bandcamp, DJ sets), profile labels/artistes, note sorties selon goût. Cible multi-utilisateur (chantier en cours) → `docs/architecture.md`.

**Reprise de contexte : ce fichier suffit** — état, TODO, pièges appris ci-dessous (fusionné depuis ancien `docs/etat.md`, ménage 2026-09-08).

**Historique complet + détails techniques** : `claude_archive.md` (dump Discogs, graphe multi-niveaux, cerveau scoring, RECOS RADAR, etc.) et `docs/archive/` (anciens docs résumés ici : `etat-2026-09-08.md`, `skill-diag.md`, `skill-dev-loop.md`) — fichiers dans `.claudeignore` (non lus automatiquement) : lire explicitement (`Read <fichier>`) si détail manquant.

**Doc technique — scoring (Lot 1)** : `docs/app-overview-scoring-lot1.md` — vue
d'ensemble du moteur de notation (`radar_web/radar/scoring.py` classe `Ctx` :
wmap/affinité, ascore, label_tier_map, reco_rows/reco_index, graph_rescore,
album_score), migration config `labels`/`watchlist` (2 listes à plat) →
`label_categories` (Cœur/Aimé, `radar_web/radar/store.py`), et jobs dépendants
(`job_scan_recos`, `job_scan_veille`, `job_profile_labels`, `job_canonicalize`,
`job_fetch_collection`, `job_merge_corpus`, `job_build_graph`). Généré en session
cloud le 2026-09-11 depuis le code lu directement (pas de token/accès réseau) —
non mis à jour automatiquement, à relire si le scoring évolue. Diagramme associé :
`docs/app-diagram-brief-scoring-lot1.md` (instructions de dessin) +
`docs/app-overview-scoring-lot1.excalidraw` (rendu, 3 groupes/14 boîtes/22
flèches — ouvrir sur excalidraw.com ou l'extension VS Code).

**Doc technique — référentiel Discogs local + scoring** :
`docs/app-overview-discogs-dump-py-and-scoring-process.md` — périmètre plus
étroit/technique que le doc Lot 1 : uniquement `radar_web/radar/discogs_dump.py`
(téléchargement/parsing dump mensuel, bascule atomique, schéma SQLite,
`search_local`/`resolve_name`/`label_style_counts`/`artist_ids_for_labels`) et
`radar_web/radar/scoring.py` (classe `Ctx`, pipeline complet dump →
`wmap`/`ascore`/`reco_rows`/`album_score`) — `store.py`, `catalog_labelgraph.py`
et jobs appelants traités comme externes (mentionnés comme sources/appelants
seulement). Généré en session cloud le 2026-09-11 depuis le code lu directement
— même mise en garde, à relire si ces deux fichiers évoluent.

**Prochaine session — reprendre ici** : Lot 1 (labels Cœur/Aimé, point 37) et
Lot 2 (graphe labels global, point 38) faits. **PR #132 mergée sur `main` le
11/09** (ordre inversé, décision utilisateur : merger d'abord, vérifier sur le
VPS après déploiement — pas de merge indépendant possible sur le VPS, il suit
`main`). **Étape 0 (import TEST, point 40) faite et concluante sur le VPS le
11/09** (détail → point 40) : Lot 2 et référentiel élargi tous formats
(point 39) fonctionnent à petite échelle. **Reste avant de considérer 38/39
clos** : réimport complet en réel sur le VPS (TODO ci-dessous), pas encore
fait — échelle réelle à confirmer (19M+ sorties, temps d'exécution, proportion
labels avec parent, pertinence `MAX_LABELS_PER_ARTIST=40`). **Lot 3 fait**
(graphe DAG explicite de `Ctx`, point 41) : pas de vrai cycle trouvé dans le
code actuel (incohérence signalée à l'utilisateur, objectif reformulé en
"rendre le DAG explicite" plutôt que "casser un cycle") — `NODE_DEPS` +
`topological_order()` (vérifié à l'import) + vérification à l'exécution dans
`Ctx._memo()`, aucun changement de comportement (détail → point 41). **Point
de contrôle utilisateur en attente** avant Lot 4 (cf. TODO ci-dessous).
Puis Lot 4 (précalcul asynchrone `track_scores`, nouveau module
`scorestore.py`, séparé de `discogs_dump.py`), Lot 5 (UI lit tables
précalculées). Livraison lot par lot, point de contrôle utilisateur après
chacun (arbitré, cf. point 37) — ne pas enchaîner plusieurs lots sans
validation entre-temps.

**Avant de lire un document non listé ici** (nouveau fichier, `docs/archive/`, `claude_archive.md`) : demander à l'utilisateur si pertinent.

**Mode caveman par défaut** : invoquer skill `caveman` (`.claude/skills/caveman/SKILL.md`, niveau `full`) en début de session, sauf demande contraire. S'applique aux réponses conversationnelles ; code, commits, doc, tickets restent en prose normale (cf. Boundaries du skill).

## État actuel du projet — résumé

1. **Structure** : `radar_web/` (FastAPI + HTMX, port 8600, interface) ; `crate_jobs.py` (tâches longues, lancées par `radar_web/worker.py`) ; `archive/` (ancienne appli Streamlit, retirée 2026-09-01, **ne pas y toucher**). Nav : Mes sources · Chercher un disque · Nouveautés · Mes labels & artistes · Reco Radar · Réglages.
2. **Déploiement** : `git push` → GitHub (`alixclaudel-hue/radar`) → merge sur `main` → `.github/workflows/deploy.yml` (VPS OVH : `git pull` + rebuild Docker + health-check, rollback si KO — rollback impossible si échec avant `reset --hard`, ex. disque VPS saturé, cf. piège backup point 12). Manuel (dépannage) : **toujours `git fetch` avant `reset --hard origin/main`** ; `--force-recreate` si conteneur reste "Running" après changement d'env. Coordonnées VPS : Secrets Actions + note perso non versionnée.
3. **Données** : `/data` sur VPS (JSON — labels, corpus, graphe, profils, config avec token Discogs). Rien dans git. Local : `export CRATE_DATA_DIR=$PWD/data`.
4. **Session cloud (celle-ci)** : dépôt cloné frais, **sans** `/data`/`.env`/token Discogs, ni Playwright/yt-dlp, ni accès SSH VPS, ni mémoire perso Claude — ce fichier + `docs/` = source de vérité. Marche à suivre : éditer, `py_compile`, smoke test des routes, ouvrir des PR. Réseau **Trusted** = registres de paquets + GitHub uniquement (passer en **Custom** + `api.discogs.com`/`bandcamp.com` pour appel réel). Sans accès OVH Manager/identifiants VPS : dépannage VPS = guider l'utilisateur pas à pas, jamais demander identifiants.
5. **Boucle diag VPS — en pause depuis 2026-09-06** (jugée non fonctionnelle par l'utilisateur, latence de livraison jamais fiabilisée). Trigger `diag-vps` désactivé (`enabled: false`) : **ne pas réactiver, ne pas ouvrir d'issue `Diag <sha>`, ne pas appeler `fire_trigger` dessus**, sans demande explicite. Reprise possible plus tard. Contrats (gelés, gardés pour référence, **déplacés hors `.claude/skills/` donc non invocables** en l'état) : `docs/archive/skill-diag.md` + `docs/archive/skill-dev-loop.md`. Pour réactiver `/diag`/`/dev-loop`, les replacer dans `.claude/skills/<nom>/SKILL.md`. Session `session_01KbkY8jHGMbLLgkkQb8Kj6d` (« Radar — VPS (diagnostic) ») reste utilisable manuellement par l'utilisateur, hors boucle.
6. **Conventions** : `py_compile` + smoke test local avant chaque push (double de la CI) ; **jamais `git add -A`** (ajouter fichiers nommément, relire `git status`) ; commits/commentaires **en français** ; pas de commentaires superflus (le *pourquoi*, pas le *quoi*).
7. **Piège — Marketplace Discogs** : prix/annonces/décompte FR **inobtenables** (Cloudflare bloque). Abandonné — garder lien `🇫🇷 voir` + pastille API.
8. **Piège — Streamlit** : archivé et mort, jamais y reporter d'évolutions.
9. **Piège — Bandcamp** (`bcsearch_public_api`) : endpoint non documenté, peut disparaître ; repli URL de recherche suffit, pas de "vraie" API depuis 2022.
10. **Piège — `discogs_get()`** : ne lève jamais d'exception, renvoie `{}` sur échec.
11. **Piège — dump Discogs** : `data.discogs.com` sert via `?download=...` (paramètre
    requête), pas le chemin direct — déjà corrigé dans `dump_url()`/`checksum_url()`.
12. **Piège — backup quotidien pouvait saturer disque VPS** (`scripts/backup.sh`) :
    archivait tout `/data` sans distinction, y compris le référentiel Discogs local
    (`shared/discogs_dump.sqlite3`, ~3G, reconstructible depuis le dump mensuel) →
    croissance anormale des archives (672M → 25G en 4 jours, cause exacte non identifiée)
    → disque plein → `git fetch` en échec au déploiement (`No space left on device`,
    incident du 2026-09-09 ayant bloqué la PR #77). Corrigé (PR #78) : dump exclu du
    `tar` + relevé de taille par sous-dossier ajouté à `backup.log` (diagnostic sans SSH).
    À surveiller : la vraie source de la croissance des backups restants n'est pas
    confirmée — vérifier `backup.log` après le prochain run (04:00 UTC).
13. **Piège — recherche YouTube** (`ytcache.search_video`) : premier résultat peut être
    une vidéo supprimée/privée ou hors-sujet. Corrigé : vérifie 15 résultats, score
    par métadonnées (titre/chaîne/description vs artiste/titre/label), cf. point 25.
14. **Référentiel Discogs local** (`discogs_dump.py`) : index SQLite du dump mensuel
    (catalogue seulement, pas le marketplace), reprenable, bascule atomique. Alimente
    recherche locale, ranking labels/artistes, graphe de co-crédits multi-niveaux
    (mode `taste` = graines Cœur+Aimés+corpus). Détail complet → archive.
15. **Entretien de fond** (`RADAR_AUTO_MAINTENANCE=1`) : `canonicalize` (hebdo),
    `profile_labels` (hebdo), `build_graph` mode `taste` (mensuel) — plus de boutons
    dans Réglages, tout automatique.
16. **Le "cerveau"** (`scoring.py`, classe `Ctx`) : `album_score`, `ascore`, `reco_rows` —
    recalculé à chaque requête depuis `/data` (cache mtime).
17. **Jobs** (`crate_jobs.py`) : tâches longues en sous-processus, statuts dans
    `/data/jobs/*.status.json`, reprenables via `*.state.json`. Ne pas rebuild le
    conteneur pendant qu'un job tourne.
18. **Mes retours** (page feedback, issue #62) : bouton 🗑 pour supprimer note déjà
    traitée — supprime à la fois côté appli (`ui_notes.json`) et le commentaire GitHub
    correspondant sur l'issue #62 (best-effort, comme l'envoi initial).
19. **RECOS RADAR** (feature sept. 2026, refondue le 09/09) : playlist **interne à
    Radar** (`recos_playlist.json` par utilisateur), pas une vraie playlist YouTube —
    aucun compte Google à connecter, aucune app OAuth à enregistrer. Page dédiée
    `/reco-radar` (même niveau de nav que Mes sources/Réglages) : lit le fichier et
    joue les vidéos via l'**API IFrame Player** YouTube côté client (`cuePlaylist`
    avec la liste d'ids vidéo). Jobs `scan_recos` (candidats notés par `album_score`
    sur les sorties des labels suivis) → `publish_recos` (chaînés ; recherche vidéo
    via `ytcache`, seule dépendance réseau restante — clé API YouTube déjà utilisée
    ailleurs, pas d'écriture). Plafond **5 pistes** (limite de test, 10/09 — était 100,
    cf. `RECOS_MAX_TRACKS` dans `crate_jobs.py` ET `radar_web/app.py`, à resynchroniser
    si modifié), **FIFO** : au-delà, la plus ancienne est retirée avant d'ajouter la nouvelle (retrait par ancienneté, pas par
    écoute réelle — l'ancien lot 3, nettoyage par scraping Playwright de l'historique
    de visionnage, a été abandonné : `ytwrite.py`, `ytwatch.py`,
    `scripts/export_youtube_session.py` et le job `clean_recos` supprimés). Suppression
    manuelle d'une piste (10/09) : bouton 🗑️ sur `/reco-radar` (`POST /reco-radar/delete`,
    par `video_id`) — retire de `recos_playlist.json` uniquement, `recos_history.json`
    conservé (la vidéo ne sera pas réajoutée automatiquement). Rechargement complet de
    page (pas htmx) : le lecteur IFrame charge sa liste une fois au chargement, une
    suppression en place le désynchroniserait des lignes. Bouton « 🗑️🔄 Forcer (tout
    rescanner) » (10/09, phase de test) : `scan_recos` avec `force=1` — vide
    `recos_seen.json` ET `recos_candidates.json` avant de scanner, pour reconstruire la
    file d'attente à neuf avec le scoring courant (ex. après un changement de
    pondération) au lieu de ne repérer que les sorties jamais vues. Ne touche ni
    `recos_playlist.json` (déjà publié) ni `recos_history.json`. Cadence auto revue
    (10/09, `worker._maybe_recos_scan`, `RADAR_RECOS_SCAN=1`) : c'était un scan fixe une
    fois par jour (donc au mieux 5 pistes/jour même file d'attente pleine, cf.
    `RECOS_MAX_ADD_PER_RUN`) — vérifie maintenant `recos_candidates.json` toutes les
    heures, lance `publish_recos` (draine la file) si elle n'est pas vide, `scan_recos`
    (qui rechaîne lui-même `publish_recos`) seulement si elle l'est. Dédoublonnage par
    IDENTITÉ DE PISTE (artiste+titre canonique, pas juste le `video_id` de la 1re
    recherche) contre TOUT ce qui a déjà été publié un jour — `recos_history.json`
    (jamais purgé) enrichi avec artiste/titre par entrée, pas seulement le `video_id`
    (`_recos_history_load`/`_recos_history_track_keys`) — donc une piste sortie de la
    playlist depuis (FIFO ou suppression manuelle) ne peut plus revenir, même si un
    nouveau match YouTube tombe sur un `video_id` différent (retour utilisateur
    2026-09-10 : « pas forcément dans la playlist à l'instant, elle a pu être
    supprimée »). Anciennes entrées d'historique (avant ce correctif, simples chaînes
    `video_id`) restent dédoublonnées par vidéo seulement, pas par identité de piste
    (pas d'artiste/titre à en tirer). **Job automatique en pause depuis le 10/09**
    (retour utilisateur, phase de test avec le plafond réduit à 5) : `_maybe_recos_scan`
    retourne immédiatement (`return` ajouté en tête de fonction, avant même la
    vérification de `RADAR_RECOS_SCAN` — variable d'environnement du `.env` VPS, hors
    d'atteinte depuis cette session cloud) — même principe que la pause de la boucle
    diag VPS (point 5). Pour reprendre l'alimentation automatique : retirer ce `return`
    dans `worker.py`. En attendant, alimentation uniquement manuelle depuis
    `/reco-radar` (« 🔄 Scanner maintenant », « ▶ Alimenter la playlist », « 🗑️🔄
    Forcer »). Wantlist RADAR (2ᵉ feature du même chantier) : pas commencée.
20. **Chantier multi-utilisateur** (détail complet → `docs/architecture.md`) : étapes 0-7
    faites et déployées (dossiers par utilisateur, comptes, file de jobs, cache YouTube
    partagé, backups chiffrés, Streamlit retiré). Bloqué sur l'étape 4 (HTTPS + domaine —
    besoin d'un nom de domaine pointant sur le VPS), puis l'étape 5b (OAuth
    Discogs/Spotify — exige aussi 2 apps développeur enregistrées). À la reprise de 5b,
    envisager `/model` Opus pour l'implémentation OAuth + toute migration de schéma.
21. **TODO opérationnel** : lancer scan vendeurs une fois à la main (Réglages →
    Catalogue de vendeurs, ~1 h pour 141 vendeurs), puis poser `RADAR_SELLER_SCAN=1` sur
    le service `radar-worker` (compose, pas le `.env` VPS) pour activer le scan hebdo
    automatique.
22. **TODO code identifié** : multi-selects genre/style, `<label for>` non reliés
    (~30 champs), libellés FR dans Réglages, liens `/disco` depuis reco/recherche, UI de
    revue des artistes « approx », suppression par track/DJ dans Mes sets. Retirés de
    cette liste (déjà faits, constaté le 10/09 en reprenant cette TODO) : pagination
    table artistes (`univers_artists_table`, `_paginate`/`ARTISTS_PAGE_SIZE`,
    `radar_web/app.py`) et composant CSS `.tbl` partagé — `review.html`/`learn.html`
    étaient les 2 seuls partials avec table encore stylée en inline au lieu de la
    classe `.tbl` (`app.css`), corrigé.
23. **Piège — `step` HTML5 sans `min`** (`settings.html`) : le pas (`step="0.05"`) prend la
    valeur initiale du champ comme base si `min` est absent — un poids par défaut non
    multiple de ce pas (ex. `artist_score.corpus: 0.18`) rend le champ invalide dès qu'on
    le modifie via la toile de pondération, ce qui bloque tout le formulaire en silence
    (champ masqué une fois la toile chargée → aucune bulle d'erreur visible, "Enregistrer"
    ne fait rien). Corrigé (PR #85) : `step="any"` sur les 7 groupes de poids de Réglages —
    à vérifier sur tout nouveau champ de poids ajouté.
24. **Boutiques vérifiées en vente** (`radar/volumo.py`, résultats de recherche, PR #86) :
    le bouton 🛍️ ne montre un lien que si le disque est confirmé en vente (plus de liste
    statique de recherche à l'aveugle, cf. retour issue #62 du 09/09). Volumo a une vraie
    API JSON publique et stable (`/api/v1/{tracks,albums}/search`, utilisée par leur propre
    site Next.js) → seule boutique intégrée. Écartés après vérification réseau réelle :
    Beatport et Traxsource (Cloudflare, challenge JS, même piège que le Marketplace
    Discogs point 7), Bleep (AWS WAF), 7digital (`/search` bloqué par AWS WAF/captcha même
    une fois le sous-domaine régional `us.7digital.com` atteint), Junodownload (site fermé).
25. **Pertinence recherche YouTube** (`ytcache.search_video`, renforcé le 10/09) : v1
    prenait le 1er résultat aveuglément ; v2 scorait par recouvrement de mots sur 5
    résultats mais restait insuffisant (retour utilisateur : manuellement on trouve
    mieux). V3 (actuelle) : requête enrichie avec le label (très discriminant en musique
    électronique), 15 résultats au lieu de 5 (même coût — `search.list` = 100 unités
    quel que soit `maxResults`), description snippet incluse dans le scoring (0 quota
    supplémentaire), bonus chaîne = artiste (+0.1) et chaîne = label (+0.2), et
    deuxième essai sans le label si le premier ne donne rien (100 unités de plus,
    seulement en cas d'échec). Repli permissif supprimé quand artiste/titre fournis :
    une mauvaise vidéo est pire que pas de vidéo (retour utilisateur confirmé).
    Recherche générique `/yt/first` (pas de données structurées) garde l'ancien repli.
26. **Piège — lien artiste↔label sans croisement de style** (`Ctx.artist_label_signal`,
    terme `label_link` de `ascore`) : partager un label suivi (base/watchlist) suffisait à
    booster n'importe quel artiste, même hors-style (ex. featuring rap sur un label
    par ailleurs house) — poussait des artistes hors-goût dans RECOS RADAR (retour
    utilisateur du 10/09). Corrigé : `discogs_dump.artist_ids_for_labels` renvoie aussi
    les styles DE LA SORTIE créditée précise (`release_styles`, pas le profil agrégé du
    label) ; `_compute_artist_label_signal` ne compte que les crédits dont au moins un
    style est dans `wmap` (goût courant) — crédit conservé tel quel si la sortie n'a
    aucun style renseigné (tag manquant, indécidable, même logique que le point N4 de
    l'archive : ne pas confondre "non classé" et "goût opposé"). Limité à ce signal
    direct ; le graphe de co-crédits (`job_build_graph`, vraies collaborations sur une
    même sortie) n'est pas concerné — piste distincte si le souci persiste après
    reconstruction du graphe (`build_graph` mode `taste`, mensuel).
27. **Piège — artiste "Various" pénalisé dans la recherche YouTube RECOS RADAR**
    (`job_scan_recos`, PR #112 mergée le 10/09) : pour les compilations, Discogs
    renvoie "Various" comme artiste de la sortie — `job_scan_recos` passait ce mot
    tel quel à `ytcache.search_video()` comme artiste de la piste. `_best_match`
    pénalise (×0.5) tout résultat où l'artiste n'apparaît pas dans les métadonnées
    vidéo (titre/chaîne/description) — or "Various" n'apparaît jamais, puisque ce
    n'est pas un vrai nom d'artiste. Combiné au seuil `MIN_MATCH_SCORE = 0.5`, ça
    rejetait quasi toutes les pistes de compilation ("aucune vidéo trouvée").
    Diagnostiqué via le journal `publish_recos` fourni par l'utilisateur (90/91
    candidats "Various" en échec, playlist bloquée à 1/5). Le quota YouTube est
    journalier, pas hebdomadaire comme initialement suspecté par l'utilisateur —
    mais l'écarter était prématuré : le journal ne pouvait pas le montrer (cf.
    point 28, écrit après le run du 10/09 qui a échoué à nouveau, cette fois avec
    les vrais noms d'artistes). Corrigé : `_track_credit_artist()`
    (`crate_jobs.py`) utilise le crédit réel par piste que Discogs fournit déjà
    (`tracklist[].artists`, rempli précisément pour ce cas), repli sur l'artiste de
    la sortie si absent (sorties normales, comportement inchangé). **File
    d'attente déjà scannée avant ce correctif à régénérer** : cliquer « 🗑️🔄
    Forcer (tout rescanner) » sur `/reco-radar` une fois le déploiement fait, sinon
    les candidats gardent l'ancien "Various" figé dans `recos_candidates.json`.
28. **Piège — échec de recherche YouTube indistinguable d'un quota épuisé**
    (`ytcache.search_video`, corrigé le 10/09) : `search_video` avalait
    `QuotaExhausted` (`except QuotaExhausted: break`), puis mettait le résultat vide
    en cache 7 jours comme un vrai « pas de match ». Conséquences vues en réel
    (journal `publish_recos` du 10/09, 92 candidats, 0 ajout) : le quota épuisé
    s'affichait « aucune vidéo trouvée » pour chaque piste, la file de candidats se
    vidait entièrement pour rien (`remaining` vide, 0 en attente), et chaque piste
    restait gelée 7 jours même une fois le quota revenu. Le `except
    ytcache.QuotaExhausted` de `job_publish_recos` était donc du code mort. Trois
    correctifs : (a) `QuotaExhausted` remonte à l'appelant (dans `search_video` comme
    dans `_best_match`) — la file est préservée et le job dit « Quota YouTube
    épuisé » ; (b) `NEG_TTL = 6 h` pour les résultats vides (`cache_get` accepte un
    `empty_max_age` distinct), les succès gardent 7 jours ; (c) nouvelle
    `search_video_diag()` qui renvoie `(video_id, raison)` — `job_publish_recos`
    journalise la raison (« aucun résultat », « meilleur score 0.32 < 0.5 : <titre
    de la vidéo vue> », « erreur API /videos »), sans quoi aucun journal ne permet
    de trancher entre quota, indexation et scoring trop strict. `search_video` reste
    l'API simple (video_id ou None) pour `/yt/first`.
    Corollaire quota : `RECOS_DAILY_SEARCH_BUDGET` passe de 90 à 45 — une piste qui
    ne trouve rien coûte 202 unités (recherche avec label + sans label + `/videos`),
    pas 100, donc 90 candidats en échec brûlaient ~18 000 unités pour un quota
    gratuit de 10 000/jour. Réserve : c'est l'explication la plus cohérente avec le
    journal (échec de TOUTES les recherches réseau alors que les entrées déjà en
    cache répondaient), **pas une preuve** — le journal d'avant le correctif ne
    disait pas la cause. Le prochain run tranchera.
29. **Piège — suffixes de désambiguïsation Discogs dans la requête YouTube**
    (`_strip_discogs_suffix`, `crate_jobs.py`) : Discogs numérote les homonymes
    (« iO (12) », « The Vision (16) », « Rhythm (2) »). Ces numéros partaient tels
    quels dans la requête YouTube et dans les jetons de scoring, où ils n'apparaissent
    dans aucune métadonnée vidéo. Retirés artiste par artiste (le crédit peut être
    une liste : « Eliza Rose (4), The Trip (8) ») pour la recherche seulement —
    l'`artist` stocké dans `recos_candidates.json`/`recos_history.json` garde le nom
    Discogs d'origine, sinon le dédoublonnage par identité de piste (point 19)
    changerait de clé et laisserait revenir des pistes déjà publiées.
30. **Piège — budget de recherche YouTube calé sur les ajouts réussis, pas les
    recherches** (`job_publish_recos`, `RECOS_MAX_ADD_PER_RUN` renommé
    `RECOS_SEARCHES_PER_RUN`, corrigé le 10/09, retour utilisateur) : la boucle
    s'arrêtait après 5 pistes **ajoutées**, pas après 5 **recherches** — si la
    plupart des candidats échouaient à matcher (cf. points 27/28 : 92 candidats
    "Various" en échec), elle continuait à chercher sur YouTube pour tout le reste
    de la file avant de s'arrêter, brûlant le quota journalier pour 0 ajout. Le
    plafond de test (5) avait justement été mis en place pour "réduire la conso
    quota" (commentaire d'origine) mais ne le faisait pas dans ce cas. Corrigé :
    la boucle s'arrête après `RECOS_SEARCHES_PER_RUN` recherches réseau tentées
    (succès ou échec), pas après 5 ajouts — un candidat déjà identifié via
    l'historique (`ck in history_keys`, aucun appel réseau) ne compte pas dans ce
    budget. Effet : une alimentation ne cherche plus jamais que sur les ~5
    premiers candidats de la file par lancement, quoi qu'il arrive. Vérifié par
    smoke test hors-ligne (`ytcache.search_video_diag` simulé, 5 échecs suivis de
    15 candidats qui auraient matché) : exactement 5 recherches effectuées, 15
    candidats laissés en attente pour le prochain run — pas testé contre l'API
    YouTube réelle (pas d'accès réseau/token depuis cette session cloud).
31. **Piège — bouton "Scanner maintenant" forçait un rescan complet à chaque clic**
    (`patte_run`, `radar_web/app.py`, corrigé le 10/09, retour utilisateur +
    journaux `scan_recos.log` à l'appui) : le bouton « 🔄 Scanner maintenant » de
    `/reco-radar` poste sur `/patte/run/scan_recos`, dont le handler appelait
    `job_launch(job)` — un appel Python **direct**, pas un passage par FastAPI.
    Le paramètre `force: str = Form("")` de `job_launch` gardait alors son
    marqueur `Form("")` non résolu au lieu de la chaîne vide, et
    `bool(Form(""))` vaut **True** (objet FastAPI, pas la valeur par défaut
    qu'il représente) — donc `if force:` dans `job_launch` était toujours vrai,
    et TOUT job lancé via `/patte/run/<job>` tournait avec `force=True` en
    permanence. Pour `scan_recos`, ça revient à cliquer "Forcer (tout rescanner)"
    à chaque clic de "Scanner maintenant" : `recos_seen.json` et
    `recos_candidates.json` vidés avant chaque scan, d'où les mêmes 20 sorties
    (mêmes scores, même ordre) et la même file de 56 candidats produits deux fois
    de suite à l'identique — confirmé en comparant les journaux fournis par
    l'utilisateur pour 2 scans consécutifs. Le point 19 (doc RECOS RADAR) et ma
    réponse précédente supposaient à tort que ce bouton respectait le cache et
    qu'un usage répété de "Forcer" en était la cause — les journaux ont infirmé
    cette piste. Corrigé : `patte_run` appelle `job_launch(job, force="")`,
    fixant explicitement l'argument en Python plutôt que de compter sur le
    défaut `Form(...)`, qui n'est résolu que lors d'un appel HTTP réel passant
    par FastAPI. N'affectait que `scan_recos` parmi les jobs lancés via cette
    route (seul à lire `params.get("force")` ; `fetch_collection`,
    `ingest_youtube/spotify/bandcamp` l'ignorent). Vérifié par test isolé
    (`bool(Form(""))` → `True` sans l'argument explicite, `False` avec) — pas
    testé en conditions réelles (pas de token Discogs depuis cette session
    cloud, donc pas de vrai `job_scan_recos` lancé de bout en bout).
32. **File RECOS pleine sans explication** (`job_scan_recos`, corrigé le 10/09,
    retour utilisateur : 2 scans consécutifs annonçant « file déjà pleine, 56 en
    attente » sans que la file ne descende) : quand `scan_recos` saute le scan
    faute de place (`len(candidates) >= RECOS_DAILY_SEARCH_BUDGET`), il chaîne
    bien `publish_recos` (point 30) mais celui-ci butait sur le quota YouTube
    journalier épuisé (confirmé par l'utilisateur via le journal `publish_recos` :
    « Quota YouTube (recherche) épuisé ») — comportement correct (la file se
    vide au reset du quota, minuit heure Pacifique) mais invisible : le message
    de `scan_recos` ne disait que « scan sauté », sans dire pourquoi la file ne
    descendait pas, obligeant à ouvrir un second journal (`publish_recos`) pour
    comprendre. Corrigé : nouvelle `_publish_recos_last_message()` lit le
    dernier message connu de `publish_recos.status.json` et l'ajoute au message
    de `scan_recos` (ex. « ... scan sauté, laisse la publication rattraper le
    retard. Dernière publication : Quota YouTube (recherche) épuisé — reprendra
    au prochain scan. »). Vérifié par smoke test hors-ligne (statut
    `publish_recos` simulé en quota épuisé, file à 56/45) : le message de
    `scan_recos` inclut bien la raison — pas testé en conditions réelles.
33. **Piège — candidat RECOS détruit définitivement au 1er échec YouTube**
    (`job_publish_recos`, corrigé le 10/09, retour utilisateur : « aucune vidéo
    trouvée » toujours après le correctif scoring du point précédent [9f1eb04],
    « tu n'as rien fait de nouveau ») : le `continue` du cas « vidéo introuvable »
    ne remettait jamais le candidat dans `remaining` — il disparaissait de
    `recos_candidates.json` dès le 1er échec, alors que sa sortie reste marquée
    « vue » dans `recos_seen.json` (`job_scan_recos`) et n'est donc plus jamais
    reproposée. Chaque échec de recherche perdait la piste pour toujours,
    indépendamment de la qualité du scoring : le correctif `ytcache._best_match`
    de la session précédente ne pouvait donc avoir aucun effet visible, la file
    finissant systématiquement vidée sans jamais rien publier — c'est ce
    mécanisme, pas le scoring, qui expliquait le symptôme signalé. Corrigé :
    nouvelle constante `RECOS_MAX_ATTEMPTS` (3) — candidat sans vidéo remis en
    file pour retenter au prochain lancement (le cache négatif `NEG_TTL` de
    `ytcache` expire sous 6h, laissant une chance à une meilleure indexation
    YouTube), abandonné explicitement (journalisé) seulement après 3 échecs.
    Vérifié par simulation hors-ligne (3 candidats : échec permanent / échec
    puis succès au 2e essai / succès immédiat) — le candidat « échec puis
    succès » est bien récupéré au 2e lancement au lieu d'être perdu dès le 1er.
    Non testé contre l'API YouTube réelle (pas d'accès réseau/clé depuis cette
    session cloud).
34. **Piège — les tentatives du point 33 ne recherchaient jamais rien** (`ytcache`,
    corrigé le 10/09, retour utilisateur : 5 pistes trouvables en 2 clics à la main
    toutes en « aucune vidéo trouvée (aucun match, en cache) », l'une abandonnée
    après ses 3 tentatives) : `search_video_diag` sert tel quel un résultat négatif
    en cache tant que `NEG_TTL` (6h) n'est pas écoulé — mais chaque relance d'un
    candidat (`RECOS_MAX_ATTEMPTS`, point 33) rejouait la MÊME clé de cache que le
    tout premier échec, donc lisait toujours ce même résultat négatif sans jamais
    réinterroger l'API, quel que soit un correctif de scoring déployé entre-temps
    (`9f1eb04` notamment). Les « 3 tentatives » ne cherchaient donc jamais rien de
    nouveau : le mécanisme du point 33 ne pouvait pas fonctionner. Corrigé :
    nouveau paramètre `force` sur `search_video`/`search_video_diag` — ignore un
    résultat négatif en cache (jamais un résultat positif, réutilisé tel quel,
    coût nul) pour forcer une vraie recherche. `job_publish_recos` passe
    `force=bool(c.get("attempts"))` : la 1re tentative respecte le cache normalement,
    toute relance (attempts ≥ 1) force une recherche réelle. Vérifié par smoke test
    hors-ligne (cache négatif pré-rempli : 0 appel réseau sans `force`, appel réel
    déclenché et vidéo retrouvée avec `force=True`). Effet secondaire : une piste
    déjà abandonnée après ses 3 tentatives AVANT ce correctif reste perdue
    (`recos_seen.json` la marque « vue ») — seul « 🗑️🔄 Forcer (tout rescanner) »
    la refait réapparaître comme nouveau candidat (attempts repart à 0). Non testé
    contre l'API YouTube réelle (pas d'accès réseau/clé depuis cette session cloud).
35. **Piège — scan RECOS toujours daté de l'année en cours** (`job_scan_recos`, corrigé
    le 10/09, retour utilisateur : « toutes les tracks sont de 2026 ») :
    `dd.search_local(label_keys=<tous les labels suivis>, limit=5000)` était UNE
    requête globale `ORDER BY year DESC LIMIT 5000` — un label prolifique l'année en
    cours remplit à lui seul les 5000 lignes, écrasant les autres labels suivis
    (même mieux notés, même possédés) qui n'atteignent jamais `Ctx.album_score` : pas
    un défaut de scoring, un défaut d'alimentation en amont (même classe de bug que le
    diagnostic D6 déjà connu pour style+année sans label, jamais corrigé pour label
    seul). Les 3 critères demandés par l'utilisateur (styles aimés, meilleur score
    reco label, possédé chez ce label) sont en fait DÉJÀ dans `album_score` : style via
    `style_affinity_of`(`wmap`), score label via `reco_index` (`_compute_reco_rows`),
    possédé via `collection.label_counts` déjà dans la feature `collection` de
    `reco_index` — mais aucun n'a jamais l'occasion de s'exprimer si la sortie n'entre
    même pas dans `rows`. Corrigé : requête PAR label (`RECOS_PER_LABEL_LIMIT = 500`)
    au lieu d'une requête globale — chaque label suivi garde sa part indépendamment du
    volume des autres. Filtre style à l'affichage volontairement resté SOUPLE (pas de
    `styles=` en dur sur la requête SQL) : un style non renseigné (tag manquant) doit
    rester "non classé", pas être confondu avec "goût opposé" (même principe que N4).
    Vérifié par smoke test hors-ligne (label A : 600 sorties 2026 ; label B : 3
    sorties 2012 mieux scorées) : label B atteint désormais le scoring malgré le
    volume de label A. Non testé contre le vrai référentiel Discogs (pas d'accès
    réseau depuis cette session cloud).
36. **RECOS RADAR — 2 curseurs Réglages** (10/09, demande utilisateur) : plafond
    `RECOS_MAX_TRACKS`/`RECOS_SEARCHES_PER_RUN` de `crate_jobs.py` (jusqu'ici
    constantes figées, dupliquées dans `radar_web/app.py` pour l'affichage, cf.
    ancien point 19 "à resynchroniser si modifié") passés dans la config, réglables
    via `/settings` : `scoring.recos.searches_per_run` (recherches YouTube par
    clic « ▶ Alimenter la playlist », curseur 1-45 — plafonné au budget quotidien
    `RECOS_DAILY_SEARCH_BUDGET`) et `scoring.recos.max_tracks` (capacité FIFO de la
    playlist, curseur 1-100). Les 2 constantes du module restent en repli si la clé
    est absente d'une config jamais réglée (nouvel utilisateur). `job_publish_recos`
    lit `cfg["scoring"]["recos"]` (déjà chargé) plutôt que les constantes ; `/reco-radar`
    (affichage) lit la même clé via `_cfg()` au lieu de l'ancienne constante
    `app.RECOS_MAX_TRACKS` dupliquée — plus besoin de resynchroniser les deux fichiers
    à la main. Vérifié par smoke test hors-ligne (rendu Jinja des 2 curseurs, config
    par défaut, `job_publish_recos` avec `max_tracks=2`/`searches_per_run=3` respecte
    bien ces valeurs plutôt que les constantes 5/5, `settings_save` enregistre les 2
    nouveaux champs). Non testé en conditions réelles (pas de token Discogs/YouTube
    depuis cette session cloud).
37. **Refonte scoring — Lot 1 : labels Cœur(T1)/Aimé(T2)** (11/09, chantier de fond issu
    d'un diagnostic Opus + arbitrages utilisateur, lot 1/5) : les 2 listes à plat
    `cfg["labels"]` (base) et `cfg["watchlist"]` (veille nouveautés) remplacées par
    `cfg["label_categories"] = {"1": [...], "2": [...]}`, même modèle que
    `artist_categories`. Migration automatique au chargement (`store.load_config`) :
    ancienne base -> Cœur (signal fort déjà curé), ancien watchlist non déjà en base ->
    Aimé, dédoublonné. `scoring.py` : nouveau `Ctx.label_tier_map()` (miroir de
    `artist_tier_map()`) remplace tous les accès directs à `cfg["labels"]`/`["watchlist"]`
    (`artist_label_signal`, `reco_rows`, `graph_rescore`, `stats`). **Changement de
    comportement assumé** (simplification demandée) : `job_scan_veille` scannait
    avant SEULEMENT `watchlist` pour la règle implicite « labels suivis » — un label
    en base sans être aussi en watchlist n'était pas surveillé pour ses nouveautés.
    Depuis ce lot, TOUT label suivi (Cœur ou Aimé) l'est automatiquement, il n'y a
    plus de distinction base/veille séparée (`job_scan_veille`, `job_scan_recos`
    utilisent désormais l'union Cœur+Aimé). UI : `/univers` (tab labels) a maintenant
    un sélecteur Cœur/Aimé à l'ajout + une colonne « Catégorie » dans le tableau
    (`<select>`, `POST /univers/label/set`, exactement le même composant que la
    table d'artistes) ; `/reco/label` prend un paramètre `tier` (1/2) au lieu de
    `dest` (base/veille/both) ; import CSV labels (`/patte/import-csv?kind=labels`)
    prend désormais un sélecteur de catégorie comme l'import artistes ; export CSV
    (`/univers/labels/export`) inclut une colonne `categorie` (miroir de l'export
    artistes). Route `/univers/labels/import` (Réglages) supprimée : dupliquait
    `/patte/import-csv?kind=labels` et n'était plus liée nulle part depuis le
    nettoyage du point 19 — code mort. Labels nouvellement détectés automatiquement
    (`job_fetch_collection`, `job_merge_corpus`) atterrissent en Aimé par défaut
    (comme un import CSV sans réglage explicite), l'utilisateur les promeut en
    Cœur ensuite depuis le tableau s'il le souhaite. Vérifié par smoke tests
    hors-ligne (migration ancien format -> label_categories, dédoublonnage,
    `label_tier_map`/`stats`, les routes d'ajout/retrait/changement de tier, rendu
    Jinja de `/univers`, `/univers/labels/table`, `/univers/reco/labels`, `/patte`,
    `/veille` sans erreur). Non testé en conditions réelles (pas de token Discogs
    depuis cette session cloud) — en particulier l'effet du changement de
    comportement `job_scan_veille` sur un vrai jeu de labels. Lot 2 fait (point 38
    ci-dessous) ; lots 3 à 5 (DAG scoring / précalcul track_scores via nouveau
    module `scorestore.py` / UI sur tables précalculées) pas commencés —
    arbitrages déjà actés : précalcul release d'abord puis piste par piste pour
    le top scoré (comme RECOS), `scorestore.py` séparé de `discogs_dump.py`
    (référentiel partagé remplacé en bloc, incompatible avec des données par
    utilisateur), livraison lot par lot avec point de contrôle après chacun.
38. **Refonte scoring — Lot 2 : graphe labels global** (11/09, suite du point 37,
    lot 2/5) : nouveau module `radar_web/radar/catalog_labelgraph.py` —
    cartographie label<->label sur le référentiel Discogs PARTAGÉ
    (`discogs_dump.sqlite3`, tout le catalogue importé, indépendant du goût ou
    du corpus d'un utilisateur quelconque), à ne pas confondre avec
    `radar_web/radar/labelgraph.py` déjà existant (sous-graphe visuel PAR
    UTILISATEUR dérivé de `producer_graph.json`/`job_build_graph`, consommé par
    `/univers/labels/graph/build`) — **incohérence détectée dans le plan
    d'origine** (qui proposait de réutiliser le nom `labelgraph.py` pour ce
    nouveau module) et tranchée avec l'utilisateur avant d'écrire le code :
    nom distinct `catalog_labelgraph.py`, choisi pour cohérence avec
    `discogs_dump.py` qui documente déjà ce référentiel comme « le catalogue ».
    **Deux origines de lien, distinguées par une colonne `kind` (jamais
    mélangées dans le même total)** — ajout du 11/09 après retour
    utilisateur pré-merge (cf. plus bas, incohérence "genre" détectée à
    cette occasion) :
    - `"artist"` (lien non dirigé) : deux labels partagent au moins un
      artiste crédité (toutes sorties confondues) ; requête SQL triée par
      `artist_id` sur `release_artists JOIN releases`, groupée en flux (pas
      de matérialisation de tout le catalogue en mémoire), comptage par
      paire accumulé dans un `Counter` vidé périodiquement (`FLUSH_EVERY =
      20 000` artistes) via un UPSERT SQLite (`ON CONFLICT DO UPDATE SET
      weight = weight + …`) — seul le lot de paires en attente de flush est
      en mémoire à un instant donné, jamais le graphe entier. Garde-fou
      anti-explosion combinatoire : un artiste crédité sur plus de
      `MAX_LABELS_PER_ARTIST` (40) labels distincts est écarté entièrement
      plutôt que tronqué au hasard (même logique que
      `scoring.DEFAULT_SCORING["graph"]["max_credits"]`/`node_cap` côté
      graphe par-utilisateur) — le nombre d'artistes écartés est journalisé
      dans le message de fin de job, pas juste silencieusement omis.
    - `"parent"` (lien DIRIGÉ, `a`=enfant/`b`=parent, `weight`=1 toujours) :
      hiérarchie label enfant/parent déclarée explicitement par Discogs
      (`<sublabels>` de `labels.xml`, déjà parsée par `import_labels` mais
      jusqu'ici jamais exploitée au-delà de l'affichage). A nécessité
      d'ajouter `labels.parent_id` (id Discogs, fiable) à côté de
      `labels.parent` (nom, affichage seul, fragile pour une jointure —
      collision possible après désambiguïsation "(2)"/"(3)", cf. diagnostic
      R3 de `resolve_name`) dans `discogs_dump.py`, résolu par le même passage
      `UPDATE` que `parent` (index `idx_labels_parent_id` ajouté). `catalog_labelgraph.neighbors()`
      restitue le sens via un champ `role` ("child"/"parent"/`None` pour
      `"artist"`, non dirigé).
    - **Incohérence détectée et tranchée avec l'utilisateur avant de coder**
      (deux questions) : (1) "genre" demandé par l'utilisateur comme donnée à
      extraire de `labels.xml` — **ce champ n'existe pas** dans le schéma
      réel du dump Discogs (confirmé par grep du code d'import existant :
      `import_labels` ne parse que id/name/sublabels, jamais un genre ;
      genre/style n'existe qu'au niveau `release`, cf. `_parse_release_elem`).
      L'équivalent (profil de styles d'un label, exhaustif, dérivé de ses
      sorties) existe déjà via `label_styles` (`_materialize_label_styles`,
      alimente `Ctx.label_affinities`) — décision utilisateur : rien de plus
      à faire, ce document existant couvre déjà le besoin. (2) "labels
      associés" (parent/sous-labels) confirmé réel et déjà parsé, mais
      jamais exploité — décision utilisateur : les intégrer comme 2e type
      d'arête dans `catalog_labelgraph.py` plutôt qu'une table séparée (un
      seul graphe label<->label, deux origines distinguées par `kind`).
    Stocké dans son propre fichier SQLite (`catalog_labelgraph.sqlite3` +
    `catalog_labelgraph_meta.json` sous `SHARED_DIR`, pas dans
    `discogs_dump.sqlite3` lui-même) avec la MÊME bascule atomique que
    `discogs_dump.py` (écriture dans `.new`, index + `os.replace` seulement à
    la fin) : une reconstruction interrompue ne casse jamais le graphe déjà
    servi, et ce module ne touche jamais au référentiel Discogs pendant qu'il
    sert des lectures.
    Nouveau job `build_catalog_labelgraph` (`crate_jobs.py`) : erreur propre si
    `discogs_dump` pas encore importé, no-op (« déjà à jour ») si le graphe a
    déjà été construit pour le `dump_date` courant (sauf `force=1`), sinon
    reconstruit et écrit `catalog_labelgraph_meta.json` avec le `dump_date`
    utilisé. Déclencheur dans `radar_web/worker.py`
    (`_maybe_catalog_labelgraph_build`, opt-in `RADAR_CATALOG_LABELGRAPH=1`,
    vérifié toutes les heures comme l'entretien de fond) : purement
    événementiel (compare `catalog_labelgraph.needs_rebuild(discogs_dump.get_meta())`),
    pas de cadence fixe à part cette vérification — la vraie cadence est celle
    du dump mensuel lui-même, exactement la formulation attendue (« job mensuel
    déclenché après `import_discogs_dump` »). Volontairement PAS chaîné en
    dur à la fin de `job_import_discogs_dump` (contrairement à
    canonicalize/profile_labels) : le déclencheur événementiel de `worker.py`
    suffit et se répare tout seul après un déploiement interrompu, sans
    dupliquer la logique « faut-il reconstruire » à deux endroits.
    Lot 2 = infrastructure pure : rien ne consomme encore ce graphe (prévu aux
    lots suivants, cf. point 37) — pas de changement de route ni de template
    dans ce lot.
    Vérifié par smoke tests hors-ligne (petite base SQLite en mémoire imitant
    le schéma `discogs_dump`, table `labels` avec hiérarchie parent/enfant
    incluse : comptage de paires "artist" correct, artiste prolifique bien
    écarté, arêtes "parent" correctement dirigées dans les deux sens
    (`neighbors()` sur le label enfant ET sur le parent), filtre par `kinds`,
    bascule atomique, `needs_rebuild()` ; `import_labels` peuple bien
    `parent`+`parent_id` depuis un vrai flux XML gzippé de test ; le job
    `build_catalog_labelgraph` de bout en bout contre un `discogs_dump.sqlite3`
    construit via le pipeline réel (`open_new_db`/`finalize_new_db`), message
    de fin détaillant les 2 types d'arêtes ; le déclencheur `worker.py` sur
    ses 5 cas — opt-in absent, dump absent, nouveau dump détecté, déjà en
    file, graphe déjà à jour). **Non testé à l'échelle réelle** (pas de vrai
    dump Discogs ni d'accès réseau depuis cette session cloud) : le volume
    réel de `release_artists`/`labels` sur le catalogue vinyle complet, le
    temps d'exécution du job, la proportion réelle de labels avec un parent
    déclaré, et la pertinence du plafond `MAX_LABELS_PER_ARTIST=40` restent à
    confirmer sur le VPS à pleine échelle — cf. TODO ci-dessous. **PR #132
    mergée sur `main` le 11/09.** Validé à petite échelle sur le VPS le 11/09
    via l'import TEST (point 40, `limit=5000`) : `build_catalog_labelgraph`
    sur ces 5000 sorties/labels/artistes donne 1831 labels, 3179 liens — 2258
    par artiste partagé (894 artistes utilisés, 1 écarté par
    `MAX_LABELS_PER_ARTIST=40`) et 921 par hiérarchie parent/enfant, en 1
    seconde. Les deux types d'arête fonctionnent, le garde-fou anti-explosion
    se déclenche correctement. Échelle réelle (catalogue complet, ~19M
    sorties) pas encore testée — cf. TODO.
39. **Référentiel Discogs élargi à TOUS les formats** (11/09, décision utilisateur
    après discussion des tradeoffs, suite du point 38) : `discogs_dump.py`
    filtrait jusqu'ici les sorties au vinyle 12"/LP uniquement dès l'import
    (`_is_vinyl`, `_parse_release_elem` renvoyait `None` sinon) — désormais
    TOUTE sortie est gardée (vinyle, CD, fichier audio, cassette, etc.),
    nouvelle colonne `releases.is_vinyl` (calculée une fois au parsing,
    jamais reparsée ensuite) pour qu'un consommateur qui voudrait rester
    vinyle seul puisse filtrer sans rouvrir la chaîne `format` (index
    `idx_releases_is_vinyl` ajouté, mais AUCUN filtre appliqué par défaut nulle part).
    Décision explicitement validée avec ses conséquences : `search_local`
    (recherche ciblée + `job_scan_recos`), `label_style_counts`/
    `_materialize_label_styles` (`Ctx.label_affinities`, donc l'affinité de
    style qui alimente `album_score`/`reco_rows` partout) et
    `catalog_labelgraph` (Lot 2, point 38) portent maintenant tous sur le
    catalogue complet, pas seulement le vinyle — améliore la cartographie
    label/artiste et le profil de style d'un label (discographie entière,
    pas seulement sa sortie vinyle) mais peut aussi faire remonter des
    éléments non-vinyle dans une recherche ciblée ou du profilage. Alternative
    écartée : élargir seulement pour `catalog_labelgraph` (deux vues du dump,
    plus complexe, impact confiné) — l'utilisateur a choisi l'élargissement
    complet en connaissance de cause.
    Le comptage `n_vinyl` (déjà suivi séparément de `n_total` avant ce
    changement — `n_total` comptait déjà TOUTES les sorties vues dans le flux
    XML, `n_vinyl` celles retenues) change de sens : avant, `n_vinyl` ==
    nombre de lignes réellement insérées ; maintenant `n_total` lignes sont
    TOUTES insérées, `n_vinyl` n'est plus que le sous-compte de celles
    marquées `is_vinyl=1` — `job_import_discogs_dump` (message de fin) et
    `discogs_dump_meta.json` reflètent ce nouveau sens.
    **Effet de bord non traité, à surveiller** : `job_scan_recos` (RECOS
    RADAR) va désormais voir aussi les rééditions CD/digital d'un même
    disque comme des lignes `releases` distinctes (déjà vrai avant pour
    plusieurs pressages vinyle du même disque, mais le volume de doublons
    potentiels augmente avec tous les formats) — pas de déduplication par
    disque/piste au-delà de ce qui existait déjà ; à revisiter seulement
    si ça se traduit par des candidats RECOS dupliqués en pratique.
    **Coût opérationnel** : la base SQLite résultante sera nettement plus
    grosse (le flux XML était déjà entièrement lu pour trier vinyle/non-vinyle,
    donc le temps de téléchargement/parsing ne change presque pas, mais le
    nombre de lignes insérées et la taille du fichier final augmentent) — à
    surveiller côté disque VPS (cf. point 12, incident de saturation déjà
    vécu avec le seul vinyle à ~3G).
    Vérifié par smoke tests hors-ligne (flux XML de test avec 4 sorties de
    formats différents — vinyle 12", CD, digital, vinyle 7" non éligible —
    toutes importées, `is_vinyl` correct pour chacune ; `catalog_labelgraph.build()`
    capte bien un lien label<->label qui n'existe QUE via une sortie non-vinyle,
    ce qui aurait été invisible avant ce changement). **Non testé à l'échelle
    réelle** (pas de vrai dump Discogs depuis cette session cloud) : la taille
    réelle de la base résultante et le temps d'import restent à confirmer sur
    le VPS à pleine échelle — cf. TODO ci-dessous. **PR #132 mergée sur `main`
    le 11/09**, même PR que le point 38. Validé à petite échelle sur le VPS le
    11/09 (import TEST, point 40, `limit=5000`) : sur les 5000 sorties
    retenues, 3568 marquées `is_vinyl=1` et 1432 non-vinyle conservées (avant
    ce lot, ces 1432 auraient été perdues à l'import) — l'élargissement
    fonctionne. Réimport complet en réel (~19M sorties) pas encore fait — cf.
    TODO.
40. **Import TEST à durée limitée** (11/09, demande utilisateur : valider les
    points 38/39 sans payer le ~1h45 d'un réimport complet) : nouveau
    paramètre `params["limit"]` sur le job `import_discogs_dump`
    (`crate_jobs.py`, `_job_import_discogs_dump_test`) — mêmes fichiers dump
    réels (téléchargement + vérification checksum inchangés, c'est le
    parsing/insertion des ~18-20M lignes qui coûte le ~1h45, pas le
    téléchargement), mais le parsing de chaque flux (releases/labels/artists)
    est coupé après `limit` éléments VUS (`discogs_dump.import_releases`/
    `import_labels`/`import_artists`, nouveau paramètre `limit`), et le
    résultat va dans un fichier SÉPARÉ (`discogs_dump.TEST_DB_PATH` =
    `discogs_dump_test.sqlite3`) — jamais `discogs_dump.sqlite3`,
    `discogs_dump_meta.json` ni `discogs_dump_import.state.json` réels, qui
    restent totalement inchangés pendant et après un run test (pas de
    résume non plus : un test interrompu se relance simplement en entier vu
    sa taille). `open_new_db()`/`finalize_new_db()` prennent un `db_path`
    optionnel (défaut `DB_PATH`) pour permettre cette bascule vers un chemin
    différent. Les `.gz` téléchargés sont volontairement CONSERVÉS après un
    run test (pas supprimés comme après un import réel) : le prochain
    réimport complet les réutilise au lieu de retélécharger plusieurs Go.
    `job_build_catalog_labelgraph` reçoit symétriquement un `params["test"]`
    (bool) : construit à partir de `discogs_dump.TEST_DB_PATH` au lieu du
    référentiel réel, écrit dans `catalog_labelgraph.TEST_DB_PATH` (nouveau,
    `catalog_labelgraph_test.sqlite3`) au lieu de `catalog_labelgraph.sqlite3`
    — `catalog_labelgraph.build()`/`_open_new_db()`/`_finalize_new_db()` ont
    un `out_path` optionnel pour ça — et ignore les garde-fous
    `available()`/`needs_rebuild()` (pas de dump/meta réel en jeu). Aucun des
    deux jobs n'est exposé dans l'UI (`VALID_JOBS` de `app.py` inchangé) :
    lancement en CLI seulement sur le VPS, ex.
    `python crate_jobs.py import_discogs_dump '{"limit": 5000}'` puis
    `python crate_jobs.py build_catalog_labelgraph '{"test": true}'` — ce
    n'est pas une fonctionnalité utilisateur, juste un outil de validation
    jetable pour ce chantier. Vérifié par smoke test hors-ligne (flux XML
    synthétiques : 3 releases/3 labels dont 1 parent-enfant/3 artists,
    `limit=2` sur chaque flux — exactement 2 éléments retenus par flux, le
    lien parent/enfant entre les 2 labels retenus correctement résolu,
    fichiers réels `discogs_dump.sqlite3`/`catalog_labelgraph.sqlite3`
    jamais créés/touchés à aucun moment). **Testé contre un vrai dump Discogs
    sur le VPS le 11/09** (dump du 20260901, `limit=5000`) : succès de bout en
    bout en 4 min 46 s (téléchargement des 3 `.gz` inclus — le poste du dump
    `artists` seul fait ~474 Mo — jusqu'à l'écriture dans
    `discogs_dump_test.sqlite3`), résultat exact : 5000 sorties (dont 3568
    vinyle 12"/LP), 5000 labels, 5000 artistes. Confirme aussi que le
    référentiel réel n'est jamais touché : le `discogs_dump.sqlite3` réel sur
    ce VPS date toujours de l'import du 2026-09-03 (avant le point 39,
    vinyle seul — 4 923 701 sorties vinyle retenues sur 19 417 067 au total),
    inchangé après ce run test.
41. **Refonte scoring — Lot 3 : graphe DAG explicite de `Ctx`** (11/09, suite
    du point 37, lot 3/5) : à la reprise de session, `CLAUDE.md` demandait de
    "casser les dépendances circulaires de `Ctx`" (diagnostic Opus antérieur,
    pas dans le repo) — analyse du code actuel (`scoring.py`) n'a trouvé
    AUCUN vrai cycle d'appel : l'ordre de calcul est déjà un DAG implicite
    (`wmap`/`artist_tier_map`/`label_tier_map` → `seed_category_weight` →
    `graph_rescore` → `artist_label_signal`/`ascore` → `label_artist_signal`/
    `label_db_signal` → `reco_rows`/`reco_index` → `album_score`), sans
    retour en arrière. Incohérence signalée à l'utilisateur avant de coder
    (cf. consigne « poser des questions si incohérence détectée ») — objectif
    réel confirmé : rendre ce DAG EXPLICITE (pas corriger un bug de cycle
    inexistant), pour préparer le précalcul asynchrone du Lot 4
    (`scorestore.py`, qui doit calculer les mêmes nœuds hors d'une requête
    HTTP, dans le bon ordre). Livré : nouveau dict `NODE_DEPS` (module
    `scoring.py`) listant, pour chaque nœud mémoïsé, les autres nœuds dont il
    dépend ; `topological_order()` calculé à l'IMPORT du module (échec
    immédiat, pas au hasard d'une requête, si `NODE_DEPS` devient
    incohérent) ; `Ctx._memo()` vérifie maintenant à l'EXÉCUTION (pile par
    thread, `threading.local`) qu'un nœud n'accède qu'à ses dépendances
    déclarées — lève une erreur explicite nommant le nœud fautif en cas de
    dérive entre le graphe documenté et un futur appel non déclaré, au lieu
    d'un `RecursionError` muet si quelqu'un introduisait un vrai cycle par
    erreur. Effet de bord découvert en construisant `NODE_DEPS` :
    `label_artist_signal` (label → artistes écoutés du corpus) n'était pas
    mémoïsé — recalculé à chaque appel, son seul appelant étant `reco_rows`
    — promu en nœud mémoïsé à part entière, sinon la vérification ci-dessus
    aurait imputé à tort son accès à `self.ascore` à `reco_rows` (l'appelant
    direct) plutôt qu'à lui-même. Clé de mémoïsation interne renommée
    `"graph_rs"` → `"graph_rescore"` (cohérence avec le nom du nœud dans
    `NODE_DEPS` ; aucun appelant externe ne référence cette chaîne). **Aucun
    changement de comportement** : mêmes formules, mêmes poids, mêmes
    signatures publiques (`app.py`/`crate_jobs.py` inchangés). Vérifié par
    smoke test (config/corpus/collection synthétiques, `CRATE_DATA_DIR`
    temporaire) : `topological_order()` détecte bien un cycle artificiel
    injecté à la main ; parcours complet de `Ctx` (wmap → reco_index via
    `album_score`) sans lever la nouvelle erreur de dépendance non déclarée ;
    scores (`ascore`, `reco_rows`, `album_score`) comparés AVANT/APRÈS (git
    stash) sur le même jeu synthétique — strictement identiques. Non testé à
    l'échelle réelle (pas de token Discogs ni de vraies données utilisateur
    depuis cette session cloud), mais aucun changement de formule ne le
    justifie ici — refactor de structure pur.

## TODO — prochaine session

- **Lot 3 (graphe DAG explicite, point 41) fait sur la branche
  `claude/hello-e87dpo`, pas encore mergé.** Pur refactor de structure (pas
  de nouvelle route/UI à tester manuellement) : relire le diff de
  `scoring.py` (nouveau `NODE_DEPS`/`topological_order()`, vérification dans
  `Ctx._memo()`), confirmer que ça correspond bien à l'objectif "DAG
  explicite" voulu, puis merger si OK avant de lancer le Lot 4
  (`scorestore.py`) — point de contrôle utilisateur requis avant
  d'enchaîner (cf. point 37).
- **PR #132 mergée sur `main` le 11/09** (Lot 2 + référentiel tous formats +
  import TEST) — déployée sur le VPS. **Étape 0 (import TEST, point 40) faite
  et concluante le 11/09** (résultats → points 38/39/40) : Lot 2 et
  l'élargissement tous formats fonctionnent à petite échelle (`limit=5000`).
  Seul point bloquant restant avant de clore ce chantier : réimport complet
  en réel ci-dessous, jamais fait à l'échelle du catalogue complet.
- **Réimport complet à pleine échelle (points 38/39)** : sur le VPS, relancer
  `import_discogs_dump` en entier (pas un `force` sur dump déjà à jour — il
  faut un VRAI réimport pour peupler `is_vinyl` et récupérer formats
  non-vinyle ; référentiel réel de ce VPS date encore du 2026-09-03, avant ce
  lot) et confirmer : taille finale de `discogs_dump.sqlite3`, temps d'import
  (comparer au ~3G/~1h45 connus pour vinyle seul — attention disque, cf.
  point 12), message de fin donnant un `n_total` très supérieur à l'ancien
  total vinyle (19 417 067) avec `n_vinyl` cohérent en sous-compte. Vérifier
  ensuite qu'une recherche ciblée (`/search`) ou `job_scan_recos` ne soit pas
  noyée de doublons CD/digital d'un même disque au point de dégrader l'usage
  réel (sinon ajouter filtre `is_vinyl=1` là où gênant plutôt que revenir sur
  l'élargissement).
- **Vérifier le Lot 2 refonte scoring à pleine échelle (graphe labels global,
  point 38)** sur le VPS, après réimport complet ci-dessus (test à petite
  échelle du 11/09 déjà concluant, cf. point 38) : activer
  `RADAR_CATALOG_LABELGRAPH=1` sur le service `radar-worker`, lancer
  `build_catalog_labelgraph` une fois à la main (référentiel Discogs déjà
  importé requis), et confirmer au journal : temps d'exécution raisonnable sur
  le vrai volume de `release_artists`, nombre de labels/liens plausible pour
  CHAQUE type d'arête (« X par artiste partagé », « Y par hiérarchie label
  parent/enfant » dans le message de fin), nombre d'artistes écartés par
  `MAX_LABELS_PER_ARTIST=40` pas disproportionné (sinon ajuster le plafond).
  Confirmer aussi qu'un second lancement sans nouveau dump répond bien « déjà
  à jour » sans rien reconstruire.
- **Vérifier le Lot 1 refonte scoring (labels Cœur/Aimé, point 37)** sur le
  VPS après déploiement : confirmer que la migration ne perd aucun label
  (comparer total Cœur+Aimé à l'ancien total base+watchlist dédoublonné), que
  l'ajout/retrait/changement de catégorie fonctionne dans `/univers`, et
  surtout que `job_scan_veille` couvre bien maintenant TOUS les labels suivis
  (Cœur ET Aimé) pour les nouveautés — pas seulement l'ancien watchlist.
- **Vérifier les 2 curseurs RECOS RADAR** (point 36) sur le VPS après
  déploiement : régler « Recherches YouTube par alimentation » et « Capacité
  de la playlist » dans `/settings`, enregistrer, puis confirmer sur
  `/reco-radar` que le plafond affiché (« X/Y ») et le comportement réel de
  « ▶ Alimenter la playlist » suivent les nouvelles valeurs (pas les anciennes
  constantes 5/5).

- **Vérifier le correctif du point 35** (scan toujours daté de l'année en
  cours) sur le VPS après déploiement : cliquer « 🗑️🔄 Forcer (tout
  rescanner) », confirmer au journal des sorties d'ANNÉES variées (pas
  uniquement l'année en cours), et que les labels à fort volume ne
  monopolisent plus la file au détriment des autres labels suivis.

- **Vérifier le correctif du point 34** (tentatives qui ne recherchaient
  jamais rien) sur le VPS après déploiement : relancer « ▶ Alimenter la
  playlist » sur les pistes actuellement à 2/3 tentatives (ex. Noir/Haze —
  Around, Candi Staton — Hallelujah Anyway, Flashmob — Need In Me, Shakedown
  — At Night) et confirmer au journal une VRAIE recherche (raison différente
  de « aucun match, en cache », ou vidéo enfin ajoutée). Pour récupérer Kings
  Of Tomorrow/Julie McKnight — Finally (déjà abandonnée avant ce correctif) :
  cliquer « 🗑️🔄 Forcer (tout rescanner) » pour qu'elle redevienne candidat
  neuf.
- **Vérifier le correctif du point 33** (candidat détruit au 1er échec
  YouTube) sur le VPS après déploiement : lancer « ▶ Alimenter la playlist »
  plusieurs fois de suite sur une file encombrée d'échecs et confirmer au
  journal qu'un candidat retenté finit par être ajouté (ou abandonné après 3
  tentatives avec message explicite), au lieu de disparaître sans trace dès
  le 1er échec — et surtout confirmer qu'au moins une vidéo est désormais
  ajoutée à la playlist (symptôme central signalé le 10/09).

- **Vérifier le correctif du point 32** (message « file pleine » sans
  raison) sur le VPS après déploiement : provoquer une file pleine (quota
  épuisé ou file > 45) et confirmer que le message de `scan_recos` affiche
  bien « Dernière publication : ... » avec la vraie raison du blocage, pas
  juste « scan sauté ».
- **Vérifier le correctif du point 31** (bouton "Scanner maintenant" forçait
  un rescan complet) sur le VPS après déploiement : cliquer « 🔄 Scanner
  maintenant » deux fois de suite sur `/reco-radar` et confirmer au journal
  que le 2ᵉ scan traite des sorties DIFFÉRENTES du 1er (ou dit "aucune
  nouveauté" si tout est déjà vu), au lieu de relister exactement les mêmes
  20 sorties.
- **Vérifier le correctif du point 30** (budget de recherche calé sur les
  recherches, pas les ajouts) sur le VPS après déploiement : lancer « ▶ Alimenter
  la playlist » avec une file encombrée de candidats voués à l'échec et confirmer
  au journal que le job s'arrête bien après 5 recherches (`RECOS_SEARCHES_PER_RUN`)
  plutôt que de vider toute la file.
- **Vérifier l'effet du correctif PR #112** (artiste "Various", point 27) sur
  le VPS après déploiement : cliquer « 🗑️🔄 Forcer (tout rescanner) » sur
  `/reco-radar`, relancer « ▶ Alimenter la playlist », confirmer que les
  pistes de compilation (ex. Aquasonic Vol. 1, Defected Classics) trouvent
  enfin une vidéo au lieu de "aucune vidéo trouvée".
- **Vérifier le journal `publish_recos` après le prochain déploiement**
  (points 28-29) : relancer « ▶ Alimenter la playlist » et lire la raison
  désormais affichée à côté de « aucune vidéo trouvée ». Si c'est « Quota
  YouTube (recherche) épuisé », attendre la remise à zéro du quota (minuit
  heure Pacifique) et relancer — la file est désormais conservée. Si c'est
  « meilleur score X < 0.5 : <titre> » sur des pistes évidentes (Kerri
  Chandler — Coro, Midland — Final Credits), le seuil `MIN_MATCH_SCORE` ou le
  cumul des pénalités de `_best_match` est trop strict : c'est alors la piste
  à travailler, pas le quota.
- **Ingestion réelle non testée depuis la factorisation** (PR #111, mergée
  sur `main` le 10/09) : `job_ingest_youtube`/`spotify`/`bandcamp`
  (`crate_jobs.py`) partagent maintenant `_ingest_lookup_loop`
  (dédoublonnage corpus, boucle `discogs_lookup`, flush tous les 15,
  `corpus_merge`, `finish`) — 43 lignes en moins ; `review.html`/`learn.html`
  réutilisent aussi la classe CSS `.tbl` (`app.css`) au lieu de styles inline
  dupliqués. Vérifié depuis cette session cloud (pas de token Discogs/
  YouTube/Spotify ici) : `py_compile` OK, smoke tests (chemins d'erreur sans
  identifiants, boucle testée avec `discogs_lookup` simulé — dédoublonnage et
  skip du lookup si label déjà connu confirmés), rendu Jinja des 2 templates.
  **Reste à faire** : tester une vraie ingestion (YouTube au moins) contre
  l'API réelle avant de considérer le skill `factorize` définitivement clos
  sur `crate_jobs.py` — depuis une session avec accès réseau + tokens (VPS ou
  réseau Custom + secrets). Reste aussi du point 22 (TODO code identifié) :
  multi-selects genre/style, `<label for>` non reliés, libellés FR Réglages,
  liens `/disco` reco/recherche, UI revue artistes « approx », suppression
  track/DJ dans Mes sets.