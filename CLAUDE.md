# Radar — instructions projet

Outil perso crate-digging vinyle sur Discogs : ingère écoute (YouTube/Spotify/Bandcamp/DJ sets), profile labels/artistes, note sorties selon goût. Cible multi-utilisateur (chantier en cours) → `docs/architecture.md`.

## ⛔ RÈGLE N°1 — DÉLÉGUER D'ABORD, NON NÉGOCIABLE

**La passerelle multi-fournisseurs (`scripts/ai_broker.py`, skill `delegate`) passe AVANT Claude sur toute tâche déléguable** — gratuit d'abord (OpenRouter gratuit, Gemini), payant (DeepSeek) seulement sur critère explicite et sous plafond journalier. Claude ne prend le relais qu'une fois la passerelle épuisée (quota, panne, sortie inexploitable), jamais « par commodité ».

| Situation | Mode | Commande AVANT toute autre action |
|---|---|---|
| Nouveau module/script/fonction | `code` | `ai_broker.py --mode code --check-syntax -o <f> "<spec>"` |
| Tests | `test` | `ai_broker.py --mode test --check-syntax -f <cible> -o <test> "<spec>"` (petits appels, un groupe de cas chacun) |
| Commit, corps de PR, note de release | `pr` | `git diff origin/main \| ai_broker.py --mode pr --stdin` |
| Log / trace / dump > 50 lignes | `diag`/`summary` | `… \| ai_broker.py --mode diag --stdin` |
| Pré-digérer un ou plusieurs documents | `context` | `ai_broker.py --mode context -f <f1> -f <f2>` |
| Localiser un symbole | `search` | `ai_broker.py --mode search --root <dir> "<question>"` |
| Raisonnement où le gratuit ne suffit pas | `reasoning` | `ai_broker.py --mode reasoning --effort high "…"` |

Tier déduit du mode (`code`/`test`/`pr`/`reasoning` → heavy, le reste → fast) ; ne forcer `-t` que pour une raison précise.

- **Gate** : le hook `PreToolUse` `scripts/hooks/delegation_gate.py` bloque commit, écriture d'un test ou d'un nouveau `.py`, `git diff` brut non relayé, `Read` non chirurgical d'un fichier > 80 lignes (`limit` ≤ 30 passe) et tout `Grep`, tant qu'aucun reçu récent (< 1 h, prompt ≥ 50 car.) du mode adéquat n'existe dans `.claude/gemini-receipts.jsonl`. Une tentative **ratée** écrit aussi son reçu et débloque : il faut avoir essayé, pas supposé. Reçu `paid` sans justification (`reasoning` ou escalade tracée) signalé en télémétrie.
- **Non délégable** : le JUGEMENT (produit, architecture, sécurité) et la relecture finale du CODE produit (`--check-syntax` prouve que ça compile, pas que c'est juste). La prose du mode `pr` est postée TELLE QUELLE, sans relecture (décision 26/09).
- **Lecture** : `context` vaut pour tout document, architecture/sécurité/`CLAUDE.md` compris. Le JSON porte `uncertain` (`{location, question}`) : Claude ne rouvre que ces endroits. Cache disque par (chemin, empreinte) — sans tenir compte du modèle ni de la question : pour plusieurs avis, `general`/`reasoning`.
- **Garde-fou** : jamais de token, clé, contenu de `.env` ou donnée personnelle dans un prompt — service externe. Filtrer logs/dumps avant de piper.
- **Tous les skills/agents intègrent la règle** (section « Délégation ») ; un skill qui ne la mentionne pas n'est pas exempté.
- **Workflow par requête** (27/09, décision utilisateur) : skill `workflow` — cadrage, WBS, ordonnancement, délégation (courtier ou sous-agent frais `executant`/`Explore` — **jamais `fork`**), consolidation par Claude.

## Conventions de session

- **Reprise de contexte : ce fichier suffit.** Historique et détails (PR, mesures, récits d'incident, textes d'origine avant compaction du 27/09) → `claude_archive.md` + `docs/archive/` (non lus auto). Avant de lire un doc non listé ici : demander à l'utilisateur.
- **Ce fichier garde les grandes lignes** (état, pièges, décisions, TODO) ; le récit d'un chantier part dans `claude_archive.md` dès qu'il est clos. Un point = 1 à 3 lignes. **Numérotation des points stable** (citée par skills et tests) : on condense, on ne renumérote pas ; un numéro absent = point retiré (texte dans l'archive), ne jamais le réattribuer.
- **Docs scoring** (générés 11/09, pas de MAJ auto) : `docs/app-overview-scoring-lot1.md` (+ brief/diagramme excalidraw), `docs/app-overview-discogs-dump-py-and-scoring-process.md`.
- **Mode caveman** (`full`) par défaut pour les réponses conversationnelles ; code/commits/doc/tickets en prose normale.
- **Vérif par défaut** : smoke test hors-ligne ; « vérifié VPS » / « conditions réelles » seulement si dit explicitement.

## État actuel du projet — résumé

1. **Structure** : `radar_web/` (FastAPI+HTMX, port 8600) ; `crate_jobs.py` + paquet `radar_jobs/` (tâches longues via `radar_web/worker.py`) ; `radar_ops/` (diagnostic, pt 43) ; `archive/` (Streamlit mort, **ne pas toucher**, jamais y reporter d'évolutions). Nav : Chercher un disque · Mes labels & artistes · Reco Radar · Réglages ; « 👤 Mon profil » et 🛒 `/wantlist` en haut à droite.
2. **Déploiement** : push → GitHub (`alixclaudel-hue/radar`) → merge `main` → `.github/workflows/deploy.yml` (VPS OVH : pull + rebuild Docker + health-check, rollback si KO — sauf échec avant `reset --hard`). Manuel : `git fetch` avant `reset --hard origin/main` ; `--force-recreate` si le conteneur reste "Running" après changement d'env.
3. **Données** : `/data` VPS (JSON + SQLite : labels/corpus/graphe/profils/config+token Discogs, référentiel Discogs local, scores précalculés). Rien dans git. Défaut `<repo>/data` (= volume `./data:/data`), `CRATE_DATA_DIR` = override facultatif.
5. **Session VPS** (contrat `.claude/skills/vps-ops/SKILL.md`) : code, PR et merge depuis `~/radar-work` ; `~/radar` (checkout Docker) et `/data` en lecture seule, tout code passe par PR + CI. **Merge = instruction explicite de l'utilisateur pour CETTE PR**, jamais par règle automatique. Peut lancer jobs, restart/rebuild Docker, activer/désactiver les drapeaux `RADAR_*` du `.env` sans accord préalable (documenté dans `radar-diag/memory/journal/`) sauf liste noire `RADAR_NO_AUTH`, `RADAR_WEB_BIND`, `RADAR_SECURE_COOKIE`, `RADAR_UID`, `RADAR_FEEDBACK_GH_TOKEN`. Boucle diag auto `diag-vps` en pause depuis 06/09 : ne pas réactiver sans demande. Mémoire opérationnelle dans le dépôt privé `radar-diag`.
6. **Conventions** : avant push, `py_compile` + `.venv/bin/python -m unittest discover -s tests` (suite entièrement verte attendue) + smoke test. **Sur le VPS, toujours `~/radar-work/.venv/bin/python`** (le Python système n'a pas `fastapi` → ~70 tests de routes en erreur) ; recréer : `python3 -m venv .venv && .venv/bin/pip install -r radar_web/requirements.txt httpx`. **Jamais `git add -A`** ; commits/commentaires **en français** ; commentaires = le pourquoi. **Piège CI** : `ci.yml` démarre un VRAI serveur (`RADAR_NO_AUTH=1`) et balaie des routes web ET `radar_ops` — toucher une route ou un drapeau de `features.py` oblige à revérifier ces listes.
7. **Piège Marketplace Discogs** : prix/annonces FR inobtenables (Cloudflare). Garder lien `🇫🇷 voir` + pastille API.
10. **Piège `discogs_get()`** : ne lève jamais, renvoie `{}` sur échec.
12. **Piège backup** (`scripts/backup.sh`) : a saturé le disque VPS deux fois — exclusion `shared/*.sqlite3*` (index reconstructibles), `.dockerignore` durci. Toute trace/journal doit être borné (rotation).
14. **Référentiel Discogs local** (`discogs_dump.py`) : index SQLite du dump mensuel complet (tous formats, tracklists), reprenable, bascule atomique. Alimente recherche locale, ranking, graphe co-crédits, précalcul des scores.
15. **Entretien de fond** (`RADAR_AUTO_MAINTENANCE=1`) : `canonicalize` et `profile_labels` hebdo, `build_graph` taste mensuel — pour chaque compte (`paths.all_uids()`, owner en tête).
16. **Le "cerveau"** (`scoring.py`, `Ctx`) : `album_score`/`ascore`/`reco_rows`, DAG explicite (`NODE_DEPS`, `topological_order`), mémoïsé par requête.
17. **Jobs** (`crate_jobs.py` = registre `JOBS` nom → module de `radar_jobs/`, seul le module lancé est importé) : sous-processus, statut `/data/jobs/*.status.json`, reprenables. **Piège tests** : patcher `cfg_load`/`discogs_get`/chemins dans le module qui définit le job, pas dans `radar_jobs.common`. Ne pas rebuild pendant qu'un job tourne. CLI-only : `prune_labels`, `reco_index`, `seller_inventory`.
18. **Mes retours** (issue #62) : 🗑 supprime la note ET le commentaire GitHub.
19. **RECOS RADAR** : playlist **interne** (`recos_playlist.json`), page `/reco-radar` (API IFrame Player). Dédoublonnage par identité artiste+titre contre `recos_history.json` (jamais purgé). Alimentation horaire auto (`RADAR_RECOS_SCAN=1`) pour chaque compte, owner en tête, pas de bouton manuel ; clé YouTube du `.env` partagée (repli `ytcache.youtube_keys`), token Discogs jamais. Piste cliquée = écoutée, purge à minuit Paris. `job_scan_recos` lit `scorestore.track_scores` (zéro réseau) ; `publish_recos` essaie d'abord la vidéo attachée par Discogs. Exclut les albums possédés (collection Discogs + achats Bandcamp, jamais la wantlist). Wantlist RADAR (playlist YouTube → wantlist Discogs) : pas commencée.
20. **Chantier multi-utilisateur** (→ `docs/architecture.md`) : étapes 0-7 faites. Bloqué étape 4 (HTTPS+domaine) puis 5b (OAuth Discogs/Spotify, 2 apps dev à enregistrer).
22. **TODO code résiduel** : multi-selects genre/style, `<label for>` non reliés (~30 champs), libellés FR Réglages, liens `/disco` reco/recherche, UI revue artistes « approx », suppression track/DJ dans Mes sets.
24. **Boutiques vérifiées** (`radar/volumo.py`) : bouton 🛍️ retiré du gabarit (22/09, « à développer plus tard »), backend intact ; réactivation = remettre le bouton dans `results.html`. Volumo seule boutique viable (les autres : Cloudflare/WAF/fermé).
26. **Référentiel tous formats** (décision utilisateur « exhaustif à la découverte, vinyle à l'achat ») : `releases.is_vinyl` ; `search_local()` filtre vinyle par défaut sauf `job_scorestore_releases` ; filtre définitif à l'ajout wantlist (`cart_add` propose un pressage vinyle).
27. **Piège `CRATE_DATA_DIR`** : défaut `<repo>/data` dans `paths.py` ET `crate_jobs.py`, chemin loggé au démarrage.
28. **File de jobs avec priorité** (`jobs.py`/`worker.py`) : `priority` 1=interactif passe devant 0=fond ; exécution sérielle à dessein (rate-limit Discogs partagé).
29. **Tracklists dans le dump local** (`discogs_dump.TRACKS_DB_PATH`) : `job_scorestore_tracks` lit le dump d'abord, API seulement si absente.
31. **Quota YouTube** (`ytcache.py`) : `QuotaExhausted` (seul le `message` Google tranche) vs `RateLimited` (retry+backoff, ~3 req/s). Budget `recos_search_budget.json` remis à zéro à minuit **Pacifique** ; ne décompte jamais une recherche ratée ; état écrit dans un `finally`.
32. **Piège `best_video_uri`** (`textmatch.py`) : pas de seuil artiste+titre global, seuil sur le titre seul + garde-fou anti dub/remix/instrumental/edit/live. Reporté : parser `<videos>` de toutes les releases d'une piste (réimport complet) — à décider après mesure du taux de match en prod.
33. **Nouveautés (/veille) et Vendeurs en PAUSE** : `features.VEILLE_ENABLED`/`SELLERS_ENABLED` = `False` (web ET worker). Réactivation = `True` + redémarrage appli+worker.
34. **Piège `label_style_counts`** : découper par lots (> 999 variables SQL → `{}` silencieux, tuait `affinity`).
35. **Cache disque `reco_index`** (`recoindex.py`) : opt-in `RADAR_RECO_INDEX=1` non activé (décision utilisateur en attente — l'entretien horaire peut retarder un clic en file).
36. **Univers de labels** (`scoring.py`) : tiers Cœur/Aimé pondérés, termes sans donnée exclus de la normalisation, composante « voisinage » (`catalog_labelgraph.py`). Non-additif, reclasse les recos.
37. **Recherche chez un vendeur** (`/search`) : stock lu en entier par `seller_inventory` (snapshot), mêmes filtres ensuite (style en OU) ; panier → annonce Discogs (pas d'API panier).
38. **`prune_labels`** : simulation par défaut, sauvegarde+rapport avant `apply=1`, jamais un label possédé/écouté. Non traité : catalogues géants type "Not On Label".
39. **Amélioration des scripts** : `script-observe` (VPS) → agent `script-doctor` → `script-loop` (corrige, remesure, PR) ; banc `scripts/loop/registry.json` + `bench.py`. Aucun correctif gardé sans un cas de banc qui échouait avant lui ; rejouer des succès de prod ne discrimine rien.
40. **PR de boucle** : `gh` + PAT (`~/.config/gh/radar_pat`) sur le VPS ; merge selon pt 5.
41. **Lot retours #62 (22/09)** : 🗑 dans le panneau « À vérifier » de `/univers` (`/univers/review/{kind}/delete`) ; `/search` à 3 modes exclusifs (catalogue · 🏷️ Mes labels · Vendeur), backend `search_run` inchangé.
43. **`radar_ops`** (service Docker séparé, port 8610, même image/volume que `radar-web`, Tailscale + `https://ops.hubclaudel.fr` via Caddy, auth propriétaire) : **lecture seule**. Pages jobs (diagramme SVG d'un pipeline sériel, réconciliation par `uid|name`), bases, fraîcheur du scoring (`scorestore_meta`, `scoring.derived_fingerprint`), journal des actions, serveur (cadran SVG façon compteur de vitesse sur la RAM de l'HÔTE — `radar_ops/memory.py` lit `/proc/meminfo`, SSE 1 s sur `/ops/stream/mem`, seuils 60/85 %), délégation (pts 57-58). Lot 3 restant : schémas de flux + skill `telemetry`.
    - **Pièges** : auth dans `radar_web/radar/websession.py`, les tests patchent `appmod.websession.dev_mode` (pas `_dev_mode`) ; journal `opslog.py` écrit DANS `_guard` d'`app.py` (pas un 2ᵉ middleware), jamais query/corps, rotation 5 Mo ; SHA via `RADAR_CODE_SHA` posé par `deploy.yml` (jamais inventé).
    - **Télémétrie** (brief `docs/brief-telemetrie.md`) : hook `scripts/hooks/telemetry.py` → `telemetry_ship.py` (branche `telemetry`, jamais sur un hook) → `telemetry_pull.py` vers `<data>/ops/remote/` (sous-dossier DISTINCT de celui du hook, sinon rejet silencieux). Commande `Bash` jamais journalisée. `RADAR_TELEMETRY_DIR` en chemin absolu (piège `~/radar` vs `~/radar-work`, pt 50).
44. **Passerelle Gemini cloud** (`scripts/gemini_gateway.py`, hook gardé par `CLAUDE_CODE_REMOTE=true`) : sans objet depuis la fermeture du cloud, inerte sur VPS.
45. **Fusion cloud + VPS (23/09)** : cf. pt 5.
46. **`/script-loop ai_query` (PR #200)** : reçus avec `error_type`/`error_detail`.
47. **Correctifs `ai_query` (PR #202)** : seuil anti gate-gaming 50 car., sortie JSON structurée.
50. **Piège `~/radar` / `~/radar-work`** : `RADAR_TELEMETRY_DIR` absolu dans `.claude/settings.json`.
51. **Retry avant repli de cascade** : `_try_with_retries()` (2 essais, backoff 4 → 30 s) sur 429/503/5xx, jamais 404 ni 4xx d'auth.
52. **Reçus** : `receipts_dir()`/`receipts_path_for()` alignés sur `RADAR_TELEMETRY_DIR`.
54. **Accès mobile** (`docs/acces-mobile-et-interfaces.md`) : une seule session tmux `claude` partagée PC/iPhone, jamais deux sessions Claude Code concurrentes sur le dépôt.
55. **Gate de lecture `Read`/`Grep`** (cf. RÈGLE N°1) : seuils `RADAR_READ_GATE_MIN_LINES` (80) / `RADAR_READ_GATE_SURGICAL_LINES` (30) ; chemin relatif résolu contre `CLAUDE_PROJECT_DIR`. Matcher `Read|Grep` activé le 26/09.
56. **Gate `git diff` brut** + repli tokenisé du mode `search` (`scripts/ai/locate.py`).
57. **Cartographie des requêtes** (`/delegation/workflows`) : `scripts/workflow_ingest.py` côté hôte (le conteneur ne voit pas `~/.claude`), relancé par le hook `Stop`, rattrapage en CLI (`--since`). **Pièges transcripts** : un appel API = plusieurs lignes JSONL au même `usage` (compter par `requestId`) ; `tool_result` dans des lignes `user` ; une `<task-notification>` prolonge la requête ; le prompt exact envoyé au modèle délégué n'est pas conservé.
58. **Courtier (27/09)** : worktree sans `.env` → repli sur le `.env` du dépôt principal (`catalogue.dotenv_paths()`) ; `--model fournisseur:modèle` accepté ; santé par contrat (`health.record_contract_failure` : sortie hors contrat → fin de cascade 24 h pour ce mode). Part déléguée = délégations abouties seulement, cache relu exclu. Repères : contexte de départ ~62k jetons, sous-agent frais ~40k, `fork` ~180k.
59. **Correctifs circuit délégation (PR #250, 27/09)** : un 403 qui ne parle que du MODÈLE (ex. `inkling-small:free` « only available on agentic harnesses ») était confondu avec une faute de clé et bannissait tout le fournisseur — distingué dans `providers.provider_fatal` ; liste noire `policy.blocked_ids` (survit au refresh mensuel du catalogue). Hook `UserPromptSubmit` (`workflow_reminder.py`) force le rappel du skill `workflow` sauf question simple sans verbe d'action. WBS du skill `workflow` : découpage désormais par LIVRABLE (les vérifs d'état vont dans le prompt délégué), plus par commande shell.

## TODO — vérifications VPS en attente

- **RECOS** : relancer `fetch_collection` + `ingest_bandcamp` puis `scan_recos force=1` — exclusion Bandcamp (pt 41) jamais vérifiée en réel.
- **Workflow par requête (pt 58)** : sur les 5 prochaines requêtes, comparer dans `/delegation/workflows` le cache relu par requête (repère 2,9 M, 137k/appel) et le nombre d'appels du fil principal — CLAUDE.md compacté le 27/09 (59 → 19 ko) à prendre en compte dans la mesure.
- **Gates (pts 55-56)** : un `git diff -- fichier` sans pipe bloque bien ; repli `search` utile à l'échelle du dépôt ; fréquence de blocage et baisse du `cache_read_input_tokens` par session (repère ~14,8 M sur 89 appels avant le gate).
- **Accès mobile (pt 54)** : `sudo ufw status numbered` (22/tcp seulement via `tailscale0`) ; chaîne iPhone Tailscale + SSH + `tmux attach -t claude` (`window-size largest` si troncature), lisibilité de `/delegation` en portrait.
- **`radar_ops` (pt 43)** : `https://ops.hubclaudel.fr/` (certificat, redirection `/login`, SSE à travers Caddy) ; débit/ETA sur un job long ; page Bases malgré le dump ; Scoring « non estampillé » → « à jour » après un `scorestore_releases` ; SSE sous charge, badge `+N`, mobile.
- **Retry Gemini (pt 51)** : observer un vrai 429/503 résorbé par le retry ; sinon throttle ~3 req/s.
- **Télémétrie (pts 43, 50)** : `~/radar-work/.claude/telemetry.jsonl` et `~/radar/.claude/telemetry.jsonl` ne grossissent plus (les supprimer ensuite) ; `/delegation` montre les deux répertoires ; surcoût du hook `PostToolUse`.
- **Perf** : temps de chargement à froid de `/wantlist` et `/patte`.
- **`vps-ops`** : périmètre du pt 5 appliqué (drapeaux documentés, merge par PR, jamais de secret en rapport).
- **Boucle scripts (pts 39-40)** : `/script-observe ytcache` puis `/script-loop ytcache` ; vérifier l'ouverture de PR et le garde-fou de merge au premier vrai cas.
- **Correctifs délégation (pt 59)** : le hook `workflow_reminder.py` déclenche bien sur requête d'action et se tait sur question chatbot en usage réel (heuristique par mots-clés, pas d'apprentissage) ; `blocked_ids` survit à un vrai `refresh_models.py` en prod ; aucun autre modèle du catalogue ne 403 avec un marqueur de restriction non couvert par `MODEL_RESTRICTED_MARKERS`.
