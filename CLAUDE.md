# Radar — instructions projet

Outil perso de crate-digging vinyle basé sur Discogs : ingère ton écoute (YouTube,
Spotify, Bandcamp, DJ sets), profile tes labels/artistes, et note des sorties Discogs
selon ton goût. Voir `docs/architecture.md` pour la cible multi-utilisateur (chantier en
cours).

**Reprise de contexte : lire `docs/etat.md`** — où en est le chantier, ce qui reste à
faire (code + opérationnel), et les pièges déjà appris.

**Historique complet et détails techniques dans `claude_archive.md`** (mécanique du dump
Discogs, graphe multi-niveaux, cerveau scoring, RECOS RADAR, etc.) — ce fichier est dans
`.claudeignore` (non lu automatiquement) : le lire explicitement (`Read claude_archive.md`)
quand un détail précis manque au résumé ci-dessous.

## État actuel du projet — résumé

1. **Structure** : `radar_web/` (FastAPI + HTMX, port 8600, l'interface) ; `crate_jobs.py`
   (tâches longues, lancées par `radar_web/worker.py`) ; `archive/` (ancienne appli
   Streamlit, retirée 2026-09-01, **ne pas y toucher**).
2. **Déploiement** : `git push` → GitHub (`alixclaudel-hue/radar`) → merge sur `main` →
   `.github/workflows/deploy.yml` (VPS OVH : `git pull` + rebuild Docker + health-check,
   rollback si KO). Manuel (dépannage) : **toujours `git fetch` avant `reset --hard
   origin/main`** ; `--force-recreate` si le conteneur reste "Running" après un
   changement d'env. Coordonnées VPS : Secrets Actions + note perso non versionnée.
3. **Données** : `/data` sur le VPS (JSON — labels, corpus, graphe, profils, config avec
   token Discogs). Rien n'est dans git. Local : `export CRATE_DATA_DIR=$PWD/data`.
4. **Session cloud (celle-ci)** : dépôt cloné frais, **pas de** `/data`/`.env`/token
   Discogs, pas de Playwright/yt-dlp, pas d'accès SSH VPS, pas de mémoire perso Claude —
   ce fichier + `docs/` sont la source de vérité. Marche : éditer, `py_compile`, smoke
   test des routes, ouvrir des PR. Réseau **Trusted** = registres de paquets + GitHub
   uniquement (passer en **Custom** + `api.discogs.com`/`bandcamp.com` pour un appel réel).
5. **Deux sessions complémentaires** : dev cloud (celle-ci — code/PR/merge, seule à
   parler à l'utilisateur) + diagnostic VPS (`session_01KbkY8jHGMbLLgkkQb8Kj6d` — lecture
   seule sur code/données réelles, ne corrige jamais). Boucle : merge → déploiement auto →
   dev cloud réveille la diag → rapport en commentaire GitHub `Diag <sha>` → dev cloud
   corrige et synthétise. Contrats : `.claude/skills/diag/SKILL.md` +
   `.claude/skills/dev-loop/SKILL.md`. Max 3 allers-retours par déploiement, puis
   escalade à l'utilisateur.
6. **Conventions** : `py_compile` + smoke test local avant chaque push (double de la CI) ;
   **jamais `git add -A`** (ajouter les fichiers nommément, relire `git status`) ;
   commits/commentaires **en français** ; pas de commentaires superflus (le *pourquoi*,
   pas le *quoi*).
7. **Piège — Marketplace Discogs** : prix/annonces/décompte FR **inobtenables**
   (Cloudflare bloque). Abandonné — garder lien `🇫🇷 voir` + pastille API.
8. **Piège — Streamlit** : archivé et mort, ne jamais y reporter d'évolutions.
9. **Piège — Bandcamp** (`bcsearch_public_api`) : endpoint non documenté, peut
   disparaître ; repli URL de recherche suffit, pas de "vraie" API depuis 2022.
10. **Piège — `discogs_get()`** : ne lève jamais d'exception, renvoie `{}` sur échec.
11. **Piège — dump Discogs** : `data.discogs.com` sert via `?download=...` (paramètre
    requête), pas le chemin direct — déjà corrigé dans `dump_url()`/`checksum_url()`.
12. **Piège — PKCE OAuth YouTube** (`ytwrite.py`) : le `code_verifier` généré par
    `authorization_url()` doit être transporté explicitement (cookie) jusqu'à
    `exchange_code()` — deux objets `Flow` distincts ne le partagent pas.
13. **Référentiel Discogs local** (`discogs_dump.py`) : index SQLite du dump mensuel
    (catalogue seulement, pas le marketplace), reprenable, bascule atomique. Alimente
    recherche locale, ranking labels/artistes, graphe de co-crédits multi-niveaux
    (mode `taste` = graines Cœur+Aimés+corpus). Détail complet → archive.
14. **Entretien de fond** (`RADAR_AUTO_MAINTENANCE=1`) : `canonicalize` (hebdo),
    `profile_labels` (hebdo), `build_graph` mode `taste` (mensuel) — plus de boutons
    dans Réglages, tout automatique.
15. **Le "cerveau"** (`scoring.py`, classe `Ctx`) : `album_score`, `ascore`, `reco_rows` —
    recalculé à chaque requête depuis `/data` (cache mtime).
16. **Jobs** (`crate_jobs.py`) : tâches longues en sous-processus, statuts dans
    `/data/jobs/*.status.json`, reprenables via `*.state.json`. Ne pas rebuild le
    conteneur pendant qu'un job tourne.
17. **RECOS RADAR** (feature sept. 2026, livrée en 3 PR) : playlist YouTube
    auto-alimentée par les nouvelles sorties des labels suivis (scoring `album_score`),
    écriture OAuth2 (`ytwrite.py`), nettoyage par scraping Playwright de l'historique de
    visionnage (`ytwatch.py`, session exportée manuellement, pas de login automatisé).
    Jobs `scan_recos` → `publish_recos` (chaînés), `clean_recos` (volontairement séparé,
    `RADAR_RECOS_CLEANUP` distinct de `RADAR_RECOS_SCAN`, pas encore validé en réel).
    Wantlist RADAR (2ᵉ feature du même chantier) : pas commencée.
