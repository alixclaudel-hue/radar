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

## TODO — prochaine session

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
