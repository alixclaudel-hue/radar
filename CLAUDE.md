# Radar — instructions projet

Outil perso crate-digging vinyle basé sur Discogs : ingère écoute (YouTube, Spotify, Bandcamp, DJ sets), profile labels/artistes, note sorties Discogs selon goût. Cible multi-utilisateur (chantier en cours) → `docs/architecture.md`.

**Reprise de contexte : ce fichier suffit** — état, TODO, pièges appris ci-dessous (fusionné depuis ancien `docs/etat.md`, ménage 2026-09-08).

**Historique complet + détails techniques** : `claude_archive.md` (dump Discogs, graphe multi-niveaux, cerveau scoring, RECOS RADAR, etc.) et `docs/archive/` (anciens docs résumés ici : `etat-2026-09-08.md`, `skill-diag.md`, `skill-dev-loop.md`) — fichiers dans `.claudeignore` (non lus automatiquement) : lire explicitement (`Read <fichier>`) si détail précis manquant.

**Avant de lire un document non listé ici** (nouveau fichier, `docs/archive/`, `claude_archive.md`) : demander à l'utilisateur si pertinent avant de lire.

**Mode caveman par défaut** : invoquer skill `caveman` (`.claude/skills/caveman/SKILL.md`, niveau `full`) en début de session, sauf demande contraire. S'applique aux réponses conversationnelles ; code, commits, doc, tickets restent en prose normale (cf. Boundaries du skill).

## État actuel du projet — résumé

1. **Structure** : `radar_web/` (FastAPI + HTMX, port 8600, interface) ; `crate_jobs.py` (tâches longues, lancées par `radar_web/worker.py`) ; `archive/` (ancienne appli Streamlit, retirée 2026-09-01, **ne pas y toucher**). Nav : Mes sources · Chercher un disque · Nouveautés · Mes labels & artistes · Reco Radar · Réglages.
2. **Déploiement** : `git push` → GitHub (`alixclaudel-hue/radar`) → merge sur `main` → `.github/workflows/deploy.yml` (VPS OVH : `git pull` + rebuild Docker + health-check, rollback si KO — rollback impossible si échec avant `reset --hard`, ex. disque VPS saturé, cf. piège backup point 12). Manuel (dépannage) : **toujours `git fetch` avant `reset --hard origin/main`** ; `--force-recreate` si conteneur reste "Running" après changement d'env. Coordonnées VPS : Secrets Actions + note perso non versionnée.
3. **Données** : `/data` sur VPS (JSON — labels, corpus, graphe, profils, config avec token Discogs). Rien dans git. Local : `export CRATE_DATA_DIR=$PWD/data`.
4. **Session cloud (celle-ci)** : dépôt cloné frais, **sans** `/data`/`.env`/token Discogs, ni Playwright/yt-dlp, ni accès SSH VPS, ni mémoire perso Claude — ce fichier + `docs/` = source de vérité. Marche à suivre : éditer, `py_compile`, smoke test des routes, ouvrir des PR. Réseau **Trusted** = registres de paquets + GitHub uniquement (passer en **Custom** + `api.discogs.com`/`bandcamp.com` pour appel réel). Sans accès OVH Manager/identifiants VPS : dépannage VPS = guider l'utilisateur pas à pas, jamais demander ses identifiants.
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

## TODO — prochaine session

- **Vérifier le correctif du point 35** (scan toujours daté de l'année en cours) sur
  le VPS après déploiement : cliquer « 🗑️🔄 Forcer (tout rescanner) », confirmer au
  journal des sorties d'ANNÉES variées (pas uniquement l'année en cours), et que les
  labels à fort volume ne monopolisent plus la file au détriment des autres labels
  suivis.

- **Vérifier le correctif du point 34** (tentatives qui ne recherchaient jamais
  rien) sur le VPS après déploiement : relancer « ▶ Alimenter la playlist » sur
  les pistes actuellement à 2/3 tentatives (ex. Noir/Haze — Around, Candi Staton —
  Hallelujah Anyway, Flashmob — Need In Me, Shakedown — At Night) et confirmer au
  journal une VRAIE recherche (raison différente de « aucun match, en cache », ou
  une vidéo enfin ajoutée). Pour récupérer Kings Of Tomorrow/Julie McKnight —
  Finally (déjà abandonnée avant ce correctif) : cliquer « 🗑️🔄 Forcer (tout
  rescanner) » pour qu'elle redevienne un candidat neuf.
- **Vérifier le correctif du point 33** (candidat détruit au 1er échec YouTube)
  sur le VPS après déploiement : lancer « ▶ Alimenter la playlist » plusieurs
  fois de suite sur une file encombrée d'échecs et confirmer au journal qu'un
  candidat retenté finit par être ajouté (ou abandonné après 3 tentatives avec
  message explicite), au lieu de disparaître sans trace dès le 1er échec — et
  surtout, confirmer qu'au moins une vidéo est désormais ajoutée à la playlist
  (le symptôme central signalé le 10/09).

- **Vérifier le correctif du point 32** (message « file pleine » sans raison) sur
  le VPS après déploiement : provoquer une file pleine (quota épuisé ou file > 45)
  et confirmer que le message de `scan_recos` affiche bien « Dernière publication :
  ... » avec la vraie raison de blocage, pas juste « scan sauté ».
- **Vérifier le correctif du point 31** (bouton "Scanner maintenant" forçait un
  rescan complet) sur le VPS après déploiement : cliquer « 🔄 Scanner maintenant »
  deux fois de suite sur `/reco-radar` et confirmer au journal que le 2ᵉ scan
  traite des sorties DIFFÉRENTES du 1er (ou dit "aucune nouveauté" si tout est
  déjà vu), au lieu de relister exactement les mêmes 20 sorties.
- **Vérifier le correctif du point 30** (budget de recherche calé sur les
  recherches, pas les ajouts) sur le VPS après déploiement : lancer « ▶ Alimenter
  la playlist » avec une file encombrée de candidats voués à l'échec et confirmer
  au journal que le job s'arrête bien après 5 recherches (`RECOS_SEARCHES_PER_RUN`)
  plutôt que de vider toute la file.
- **Vérifier l'effet du correctif PR #112** (artiste "Various", point 27) sur le
  VPS après déploiement : cliquer « 🗑️🔄 Forcer (tout rescanner) » sur
  `/reco-radar`, relancer « ▶ Alimenter la playlist », confirmer que les pistes de
  compilation (ex. Aquasonic Vol. 1, Defected Classics) trouvent enfin une vidéo au
  lieu de "aucune vidéo trouvée".
- **Vérifier le journal `publish_recos` après le prochain déploiement** (points 28-29) :
  relancer « ▶ Alimenter la playlist » et lire la raison désormais affichée à côté de
  « aucune vidéo trouvée ». Si c'est « Quota YouTube (recherche) épuisé », attendre la
  remise à zéro du quota (minuit heure Pacifique) et relancer — la file est maintenant
  conservée. Si c'est « meilleur score X < 0.5 : <titre> » sur des pistes évidentes
  (Kerri Chandler — Coro, Midland — Final Credits), le seuil `MIN_MATCH_SCORE` ou le
  cumul des pénalités de `_best_match` est trop strict : c'est alors la piste à
  travailler, pas le quota.
- **Ingestion réelle non testée depuis la factorisation** (PR #111, mergée sur `main`
  le 10/09) : `job_ingest_youtube`/`spotify`/`bandcamp` (`crate_jobs.py`) partagent
  maintenant `_ingest_lookup_loop` (dédoublonnage corpus, boucle `discogs_lookup`,
  flush tous les 15, `corpus_merge`, `finish`) — 43 lignes en moins ; `review.html`/
  `learn.html` réutilisent aussi la classe CSS `.tbl` (`app.css`) au lieu de styles
  inline dupliqués. Vérifié depuis cette session cloud (pas de token Discogs/
  YouTube/Spotify ici) : `py_compile` OK, smoke tests (chemins d'erreur sans
  identifiants, boucle testée avec `discogs_lookup` simulé — dédoublonnage et skip
  du lookup si label déjà connu confirmés), rendu Jinja des 2 templates. **Reste à
  faire** : tester une vraie ingestion (YouTube au moins) contre l'API réelle avant
  de considérer le skill `factorize` définitivement clos sur `crate_jobs.py` —
  depuis une session avec accès réseau + tokens (VPS ou réseau Custom + secrets).
  Reste aussi du point 22 (TODO code identifié) : multi-selects genre/style,
  `<label for>` non reliés, libellés FR Réglages, liens `/disco` reco/recherche, UI
  revue artistes « approx », suppression track/DJ dans Mes sets.
