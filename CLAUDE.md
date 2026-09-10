# Radar — instructions projet

Outil perso crate-digging vinyle basé sur Discogs : ingère écoute (YouTube, Spotify, Bandcamp, DJ sets), profile labels/artistes, note sorties Discogs selon goût. Cible multi-utilisateur (chantier en cours) → `docs/architecture.md`.

**Reprise de contexte : ce fichier suffit** — état, TODO et pièges appris dans résumé ci-dessous (fusionné depuis ancien `docs/etat.md`, ménage 2026-09-08).

**Historique complet et détails techniques** : `claude_archive.md` (dump Discogs, graphe multi-niveaux, cerveau scoring, RECOS RADAR, etc.) et `docs/archive/` (anciens docs résumés ici : `etat-2026-09-08.md`, `skill-diag.md`, `skill-dev-loop.md`) — fichiers dans `.claudeignore` (non lus automatiquement) : lire explicitement (`Read <fichier>`) si détail précis manquant.

**Avant de lire un document non listé ici** (nouveau fichier, `docs/archive/`, `claude_archive.md`) : demander à l'utilisateur si pertinent plutôt que lire d'emblée.

**Mode caveman par défaut** : invoquer skill `caveman` (`.claude/skills/caveman/SKILL.md`, niveau `full`) en début de session, sauf demande contraire. S'applique aux réponses conversationnelles ; code, commits, doc, tickets restent en prose normale (cf. Boundaries du skill).

## État actuel du projet — résumé

1. **Structure** : `radar_web/` (FastAPI + HTMX, port 8600, interface) ; `crate_jobs.py` (tâches longues, lancées par `radar_web/worker.py`) ; `archive/` (ancienne appli Streamlit, retirée 2026-09-01, **ne pas y toucher**). Nav : Mes sources · Chercher un disque · Nouveautés · Mes labels & artistes · Reco Radar · Réglages.
2. **Déploiement** : `git push` → GitHub (`alixclaudel-hue/radar`) → merge sur `main` → `.github/workflows/deploy.yml` (VPS OVH : `git pull` + rebuild Docker + health-check, rollback si KO — rollback impossible si échec avant `reset --hard`, ex. disque VPS saturé, cf. piège backup point 12). Manuel (dépannage) : **toujours `git fetch` avant `reset --hard origin/main`** ; `--force-recreate` si conteneur reste "Running" après changement d'env. Coordonnées VPS : Secrets Actions + note perso non versionnée.
3. **Données** : `/data` sur VPS (JSON — labels, corpus, graphe, profils, config avec token Discogs). Rien dans git. Local : `export CRATE_DATA_DIR=$PWD/data`.
4. **Session cloud (celle-ci)** : dépôt cloné frais, **pas de** `/data`/`.env`/token Discogs, pas de Playwright/yt-dlp, pas d'accès SSH VPS, pas de mémoire perso Claude — ce fichier + `docs/` = source de vérité. Marche : éditer, `py_compile`, smoke test des routes, ouvrir des PR. Réseau **Trusted** = registres de paquets + GitHub uniquement (passer en **Custom** + `api.discogs.com`/`bandcamp.com` pour appel réel). Pas d'accès OVH Manager/identifiants VPS : dépannage VPS = guider l'utilisateur pas à pas, jamais demander ses identifiants.
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
    une vidéo supprimée/privée. Corrigé : vérifie le statut des 5 premiers résultats,
    retient le premier encore lisible (`_first_playable`).
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
    ailleurs, pas d'écriture). Plafond 100 pistes, **FIFO** : au-delà, la plus
    ancienne est retirée avant d'ajouter la nouvelle (retrait par ancienneté, pas par
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
    (qui rechaîne lui-même `publish_recos`) seulement si elle l'est. Dédoublonnage
    étendu à la playlist déjà publiée (`job_scan_recos`, pas seulement la file), pour
    qu'un rescan (forcé ou non) ne remette jamais en file une piste déjà en lecture.
    Wantlist RADAR (2ᵉ feature du même chantier) : pas commencée.
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
22. **TODO code identifié (non commencé)** : pagination table artistes, composant CSS
    `.tbl` partagé (recopié dans 3 partials), multi-selects genre/style, `<label for>` non
    reliés (~30 champs), libellés FR dans Réglages, liens `/disco` depuis reco/recherche,
    UI de revue des artistes « approx », suppression par track/DJ dans Mes sets.
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
25. **TODO identifié — pertinence recherche YouTube** (`ytcache.search_video`) : prend le
    1er résultat de recherche sans vérifier la pertinence (contrairement à
    `bandcamp.py`/`volumo.py` qui scorent par recouvrement de mots artiste/titre) — cause
    de vidéos hors-sujet dans la playlist RECOS RADAR (retour utilisateur du 09/09). Piste
    de correction identifiée (même principe de score, quasi gratuit en quota car `/videos`
    est déjà appelé pour vérifier la lisibilité), pas encore implémentée.
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
