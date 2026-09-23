# HANDOVER — session cloud → session VPS (2026-09-23)

Note de passation à lire par la session Claude Code qui prend le relais sur le
VPS. La session cloud est close ; tout le développement continue désormais
directement sur le VPS (`tmux`).

## État d'avancement actuel

**Livré par la session cloud aujourd'hui (23/09/2026)** :

1. **PR #192 mergée** dans `main` (`0dd2e11a`) — résolution d'un conflit
   entre deux PR qui traitaient le même brief (repli `.env` pour lancement
   CLI direct hors `docker compose`). Deux implémentations avaient été
   proposées :
   - `_load_dotenv_fallback` (mutation `os.environ` dans `main()`)
   - `_key_from_dotenv` (lecture au niveau `query_gemini()`, plus général)

   La seconde a été gardée (#193 déjà mergée sur `main`), la première
   retirée. Suite complète 338 tests OK.

2. **Gateway Gemini local pour session cloud** — commit `a8927cc` sur la
   branche `claude/hello-e87dpo`, ouvert en PR #194. Contient :
   - `scripts/gemini_gateway.py` (stdlib pure, ~180 lignes) : mini serveur
     HTTP local qui lit la clé user dans CCR
     (`~/.claude-code-router/config.sqlite`) et l'ajoute en `?key=` (query,
     jamais header — le proxy cloud réécrit `x-goog-api-key`).
   - `scripts/cloud-gemini-gateway.sh` : hook `SessionStart` gardé par
     `CLAUDE_CODE_REMOTE=true`, sans effet hors cloud.
   - `scripts/ai_query.py` : lit `RADAR_GEMINI_BASE_URL` ou le marqueur
     éphémère `/tmp/radar-gemini-gateway.url` ; `api_key` explicitement
     passé continue de court-circuiter le gateway.
   - `tests/test_ai_query.py` : 5 tests dédiés (`GatewayRoutingTests`).
   - `.claude/settings.json` : nouveau hook `SessionStart`.
   - `.claude/skills/ask-gemini/SKILL.md` + `CLAUDE.md` (pt 44) documentés.

   Suite 343 tests OK. Preuve bout-en-bout : le message de commit du
   gateway a lui-même été rédigé par Gemini via `ai_query.py --mode pr`,
   passant par le gateway (reçu dans `.claude/gemini-receipts.jsonl`).

3. **Retour du user tardif dans la session** : l'utilisateur estime
   l'approche « gateway cloud » trop complexe et préfère centraliser
   l'exécution Gemini sur le VPS. Ce handover matérialise ce basculement.

**Ce qui reste tel quel** (rien changé cette session, à confirmer côté VPS) :

- Tous les autres chantiers de `CLAUDE.md` — voir la section
  « TODO — vérifications VPS en attente » de `CLAUDE.md`, notamment les
  pts 19 (RECOS), 34/36/37 (labels & scoring), 41 (retours issue #62),
  43 (radar_ops LOT 3 restant + télémétrie à vérifier en prod).

## Reste à faire (Next Steps)

**Priorité 1 — décisions à trancher côté VPS avant de coder** :

- **PR #194 (gateway Gemini cloud)** : à fermer ou merger ?
  - **Fermer sans merger** si le user maintient le choix « tout côté VPS »
    (le gateway devient inutile). Nettoyage : rollback local des 7 fichiers
    du commit `a8927cc` OU garder le code comme filet de secours cloud.
  - **Merger** si le gateway peut cohabiter (petite dette : deux systèmes
    Gemini en parallèle).
  - **Recommandation session cloud** : le gateway a été testé et fonctionne,
    il ne coûte rien sur le VPS (hook gardé par `CLAUDE_CODE_REMOTE=true`).
    Le laisser en place n'introduit aucun risque. À discuter avec l'humain.

- **Architecture « broker de délégation Gemini VPS » (envisagée mais non
  codée dans cette session, faute de temps)** :
  - Concept : la session cloud écrit ses briefs dans
    `docs/gemini-delegations/pending/*.json`, push. La session VPS lance
    un script `scripts/gemini_broker.py` qui traite tous les briefs
    pending, appelle `ai_query.py` localement, écrit les réponses dans
    `.../replies/`, commit+push.
  - Statut : NON commencé. Si le VPS reste le seul point d'entrée Gemini
    et que la session cloud (si réactivée) doit pouvoir déléguer, ce
    chantier reste à faire. Sinon (workflow 100% VPS), sans objet.

**Priorité 2 — vérifications en conditions réelles** :

Toutes celles listées dans `CLAUDE.md` sous « TODO — vérifications VPS en
attente ». En particulier, jamais confirmé en prod :

- RECOS : filtre albums possédés (pt 41), budget YouTube (pt 31), texte
  simplifié de `/reco-radar` (pt 41).
- Labels & scoring : impact `reco_rows` non-additif (pts 34/36), curseur
  tier Cœur/Aimé, 3ᵉ composante voisinage catalogue.
- `radar_ops` (pt 43) : service `radar-ops` sur port 8610, débit/ETA des
  jobs, page Bases sans latence sur le dump Discogs, télémétrie lot 2 (var
  `RADAR_TELEMETRY_DIR=/data/ops` pour la session VPS).
- `vps-ops` (pt 40) : PR #184 (4ᵉ élargissement du contrat) — review et
  décision merge par l'utilisateur ; provisionner `gh` CLI + PAT sur VPS
  hors dépôt git.

## Architecture & Flux de données

**Ce que fait l'application** : Radar est un outil perso de crate-digging
vinyle sur Discogs. Il ingère l'écoute (YouTube, Spotify, Bandcamp, sets DJ),
profile les labels/artistes, et note les sorties selon le goût de
l'utilisateur.

**Stack** : Python 3.11 + FastAPI/HTMX (`radar_web/`, port 8600), worker en
sous-processus (`crate_jobs.py` + `radar_web/worker.py`, file avec priorité),
SQLite pour l'index Discogs, JSON pour les référentiels utilisateur, service
sœur `radar-ops` (port 8610) en lecture seule pour le diagnostic.

**Flux principal** :
```
Écoute (YouTube/Spotify/Bandcamp) ──┐
DJ sets Playwright ─────────────────┼──> profils labels/artistes (JSON)
Collection Discogs ─────────────────┘        │
                                             ▼
                            scoring.py (Ctx, DAG mémoïsé)
                                             │
                                             ▼
                    /reco-radar (playlist interne YouTube via IFrame API)
                    /univers (labels/artistes ranked)
                    /search (recherche locale filtrée vinyle)
```

**Chaîne délégation Gemini (règle N°1)** :
```
Claude Code ──┐
              │  (VPS) : GEMINI_API_KEY exportée par docker compose
              │          ai_query.py → generativelanguage.googleapis.com
              │          → reçu écrit dans .claude/gemini-receipts.jsonl
              │
              │  (Cloud, PR #194 en attente de décision) :
              │          ai_query.py → gateway local :8632 → Google
              │          Gateway lit la clé dans ~/.claude-code-router/config.sqlite
              │
              └──> hook gemini_gate.py vérifie qu'un reçu récent existe avant
                   d'autoriser commit / écriture d'un nouveau .py / écriture
                   de test.
```

**Déploiement** (déjà en place) :
```
git push origin main
    ↓
.github/workflows/deploy.yml (GitHub Actions)
    ↓
VPS OVH : git pull → docker compose build → health-check
    ↓
Rollback automatique si le health-check échoue (sauf si l'échec précède le
reset --hard, cf. pt 12).
```

## Variables d'environnement requises

Toutes documentées dans `.env.example` (regénéré à jour dans le même
commit que ce handover). Le fichier `.env` est **volontairement hors git**
(`.gitignore` l'exclut, ainsi que ses backups `.env.bak-*` faits par la
session VPS avant de basculer un drapeau `RADAR_*`).

**Obligatoires sur le VPS** :
- `APP_PASSWORD` — accès web owner-only.
- `APP_SESSION_SECRET` — signature des cookies de session.
- `OWNER_USERNAME`, `RADAR_UID` — identité owner.
- `DISCOGS_TOKEN` — API Discogs (v2, personal token).
- `YOUTUBE_API_KEY` — quota YouTube v3 (compteur `recos_search_budget.json`).
- `GEMINI_API_KEY` — clé AI Studio gratuite pour la règle N°1.

**Optionnels (drapeaux)** :
- `RADAR_AUTO_MAINTENANCE=1` — entretien de fond hebdo (canonicalize,
  profile_labels, build_graph).
- `RADAR_RECOS_SCAN=1` — boucle RECOS auto horaire (worker.py).
- `RADAR_RECO_INDEX=1` — cache disque du reco_index (pt 35).
- `RADAR_CATALOG_LABELGRAPH=1` — 3ᵉ composante voisinage catalogue (pt 36).
- `RADAR_DISCOGS_DUMP_SYNC=1` — sync mensuelle du dump Discogs.
- `RADAR_SELLER_SCAN=1` — scan hebdo des vendeurs (en pause, cf. pt 21/33).
- `RADAR_TELEMETRY_DIR` — où le hook télémétrie écrit (défaut : `.claude/` ;
  sur le VPS mettre `/data/ops` pour ne pas salir le checkout).
- `RADAR_UID`, `RADAR_NO_AUTH`, `RADAR_SECURE_COOKIE`, `RADAR_WEB_BIND`,
  `RADAR_DOMAIN`, `RADAR_FEEDBACK_GH_TOKEN` — cf. `.env.example` et le
  contrat `.claude/skills/vps-ops/SKILL.md` (liste noire des 5 clés
  sensibles jamais basculées automatiquement).

## Commandes pour tester

**Installer les dépendances** (une session cloud fresh clone les ferait
déjà via `scripts/cloud-setup.sh`, mais sur le VPS c'est le rôle de
`docker compose build`) :

```bash
# Local ou VPS bare (hors Docker) :
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install fastapi 'uvicorn[standard]' jinja2 python-multipart requests
```

**Vérifications hors-ligne (fait avant chaque push)** :

```bash
# Compilation
python3 -m py_compile scripts/ai_query.py scripts/gemini_gateway.py

# Suite unittest complète — >150 cas, >338 avec les nouveaux (343 après
# PR #194)
python3 -m unittest discover -s tests

# Smoke test d'un appel Gemini (sur VPS, avec .env contenant GEMINI_API_KEY)
python3 scripts/ai_query.py --mode general "Réponds seulement OK"
```

**Docker sur le VPS** :

```bash
# Depuis le checkout git de prod, jamais depuis ~/radar-work (cf. contrat
# vps-ops)
cd ~/radar
git pull
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

**Endpoints disponibles une fois lancé** :
- `http://<domaine>/` — page d'accueil owner-only
- `http://<domaine>:8610/` — `radar-ops` (diagnostic, Tailscale ou
  loopback uniquement, jamais exposé par Caddy)

**Contrat pour la session VPS Claude Code** — lire d'abord
`.claude/skills/vps-ops/SKILL.md` avant toute action (périmètre autorisé,
interdits, garde-fous Docker/jobs, format de rapport, liste noire de
5 clés `RADAR_*` sensibles). L'accord préalable de l'utilisateur est
requis pour ACTIVER un drapeau `RADAR_*` ; désactiver en incident reste
autonome.

## Notes de sécurité importantes

- **Ne jamais committer `.env`** — le `.gitignore` l'exclut, mais toujours
  vérifier `git status` avant `git add .`.
- **Clé Gemini à régénérer si suspicion** : le user a signalé un
  changement récent de clé (23/09) qui pourrait expliquer certains 401 ;
  la clé actuelle stockée dans `~/.claude-code-router/config.sqlite`
  sur cette session cloud était valide (Google renvoyait 429 « quota
  atteint », pas 401). Le VPS a sa propre clé dans `.env`, distincte.
- **PR #184 en attente** (4ᵉ élargissement contrat vps-ops) : merge à la
  discrétion de l'utilisateur ; provisionner ensuite `gh` CLI + PAT
  fine-grained sur le VPS **hors dépôt git**, `chmod 600`.
