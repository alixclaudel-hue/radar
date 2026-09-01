# Architecture — Radar multi-utilisateur (Option A)

Référence partagée : humains + sessions Claude Code. Tenu à jour à chaque étape du
chantier.

---

## 1. Vue d'ensemble

**Aujourd'hui — mono-locataire.** Un seul jeu de fichiers JSON dans `/data`, un token
Discogs, un `APP_PASSWORD`. Tous ceux qui se connectent voient le même « univers ».

**Cible — Option A : dossiers par utilisateur.** On garde le modèle fichiers JSON, mais
namespacé par utilisateur : `/data/users/<uid>/…`. Vrais comptes applicatifs (sur
invitation). Chaque utilisateur connecte ses propres services (Discogs, Spotify…) et
construit son univers isolément. Cible réaliste : 3 à ~20 utilisateurs. Au-delà →
migration vers une base SQL (Option B), pas avant.

```
VPS Ubuntu  ─ Docker ─┬─ radar-web (FastAPI/HTMX, :8600)  ← seule interface active
                      │     Ctx(uid) lit/écrit /data/users/<uid>/
                      │     file de jobs (1 worker) consciente du rate-limit
                      └─ [crate-radar Streamlit :8501 — RETIRÉ au passage en A]

/data
 ├─ users/<uid>/           ← tout ce qui touche au goût d'un utilisateur
 ├─ shared/                ← caches neutres, communs à tous
 ├─ accounts.json          ← comptes applicatifs (hash argon2)
 ├─ jobs/                  ← statuts + file d'attente
 └─ backups/               ← archives chiffrées
```

---

## 2. Modèle de données — partagé vs par-utilisateur

| Fichier | Portée | Raison |
|---|---|---|
| `crate_radar_config.json` (labels, taste_categories, artist_categories, veille_rules, connexions) | **par-user** | définit le goût / la config d'un utilisateur |
| `taste_corpus.json` | **par-user** | tracks ingérées (YouTube/Spotify/Bandcamp/DJ sets) de l'utilisateur |
| `labels_profile.json` | **par-user** | profils de styles des labels de *sa* base |
| `labels_resolved.json`, `artists_resolved.json` | **par-user** | résolutions liées à sa base |
| `producer_graph.json` | **par-user** | graphe construit sur *ses* graines |
| `collection_cache.json` | **par-user** | sa collection + wantlist Discogs |
| `reco_feedback.json`, `scoring_profiles.json` | **par-user** | son apprentissage |
| `radar_web_searches.json` | **par-user** | son historique de recherche |
| `pending_enrich.json`, `veille_new.json`, `veille_seen.json`, `sellers_seen.json` | **par-user** | ses files / états de veille |
| `lookup_cache.json` (release/artiste → label/style Discogs) | **partagé** | métadonnées Discogs publiques, neutres — une résolution faite par un user sert tout le monde |
| `release_meta_cache.json` (note ★, prix, num_for_sale) | **partagé** | données de release publiques |
| `youtube_cache.json` (résultats `search`, résolutions ▶, métadonnées vidéo) | **partagé** | données YouTube publiques ; économise le quota |

**Règle :** un fichier n'est « partagé » que s'il ne contient **rien** de spécifique à un
utilisateur. Toute nouveauté est par-user par défaut ; on ne partage qu'après audit.

---

## 3. Comptes & sessions applicatives

- `accounts.json` : `{ uid: { username, pw_hash (argon2id), created_at, invited_by } }`.
  Création **sur invitation uniquement** (un compte owner génère un lien/jeton d'invitation).
- Cookie de session signé (comme l'actuel `radar_auth`), mais portant `uid`. TTL glissant.
- `APP_PASSWORD` global disparaît → login par compte.
- `_guard` (middleware) résout `uid` depuis le cookie ; toute route passe `uid` à `Ctx`.
- Pas de rôles pour l'instant (tous égaux). Un `owner` peut inviter et voir l'usage global.

---

## 4. Connexions aux services externes

Chaque utilisateur connecte ses propres comptes. Stocké dans **sa** config, **chiffré au
repos** (clé dans le `.env` du VPS, hors git).

| Service | Méthode | UX | Notes |
|---|---|---|---|
| **Discogs** | **OAuth 1.0a** (« Connecter Discogs ») | 1 clic | token+secret sans expiration. Rate-limit **par token** → 60/min par utilisateur, l'IP partagée du VPS n'est pas un problème. Collage de *personal token* gardé en secours. |
| **Spotify** | **OAuth 2.0** (« Connecter Spotify ») | 1 clic | débloque toute la bibliothèque (playlists privées, likes, top artistes). Dev-mode plafonné à 25 users, extension = formulaire gratuit. |
| **YouTube** | **1 clé API de l'appli** (`YOUTUBE_API_KEY` dans `.env`) | rien à faire | lecture seule de données publiques (playlists collées, recherche DJ sets, résolution du bouton ▶). **Cascade quota** : si `403 quotaExceeded` sur la clé appli → bascule sur la clé perso de l'user si renseignée → sinon message + champ pour en coller une. Préférer la clé perso quand elle existe (protège le pot commun). OAuth Google seulement si un jour on veut les playlists privées (scope restreint → audit Google au-delà de 100 users). |
| **Bandcamp** | **mot de passe d'app Subsonic** (identifiant + mot de passe généré, collés) | 1 collage unique | pas d'OAuth, pas d'API publique. Le Subsonic est un mot de passe **à portée limitée** (musique achetée uniquement). Chiffré au repos. Incontournable. |

**Prérequis OAuth : HTTPS + nom de domaine.** Les callbacks (`https://domaine/oauth/callback/<service>`)
ne peuvent pas pointer sur une IP Tailscale en clair. → étape bloquante, remontée en tête
du plan.

---

## 5. File d'attente des jobs

Remplace le `subprocess.Popen` fire-and-forget actuel.

- `jobs/queue.json` : file FIFO `{ id, uid, name, params, enqueued_at, status }`.
- **1 worker** (process dédié dans le conteneur) qui dépile en **round-robin entre
  utilisateurs** — le gros `profile_labels` d'un user n'affame pas les autres.
- Le worker lit `X-Discogs-Ratelimit-Remaining` sur chaque réponse, **dort** quand le seau
  est bas, **backoff exponentiel** sur `429` / `Retry-After`.
- Compteur local d'unités YouTube par clé et par jour → bascule proactive avant le 403.
- L'UI affiche « en file, position 3 » au lieu de lancer immédiatement.
- Jobs déjà reprenables (`*.state.json`) — inchangé.

---

## 6. Caches partagés

`/data/shared/` : `lookup_cache.json`, `release_meta_cache.json`, `youtube_cache.json`.
Grosse économie d'appels : après le 1er utilisateur, l'essentiel du profilage /
résolution d'un nouveau = cache hits. Éviction **LRU avec plafond d'octets** sur le cache
mémoire (`store._LOAD_CACHE`) pour éviter que N `labels_profile.json` par-user saturent la
RAM.

---

## 7. Sécurité

- **HTTPS + domaine** : reverse-proxy (Caddy ou Traefik) devant `radar-web`, certificat
  Let's Encrypt auto. Prérequis des OAuth + hygiène de base (aujourd'hui « Non sécurisé »
  en clair). L'accès Tailscale peut rester en plus pour l'admin.
- **Secrets au repos** : tokens OAuth + mot de passe Subsonic chiffrés (clé symétrique
  `age`/Fernet dans `.env`, jamais dans git). `accounts.json` = hash argon2id, pas de
  mot de passe en clair.
- **`.gitignore`** couvre `/data/`, `.env`, tous les `*.json` de données, `discogs_state.json`,
  `discogs_profile/`. `git add -A` est proscrit — ajouter les fichiers nommément.
- **`.env` VPS** : `APP_SESSION_SECRET`, `DATA_ENCRYPTION_KEY`, `YOUTUBE_API_KEY`,
  identifiants OAuth de l'appli (Discogs/Spotify), `VPS_*` pour le déploiement CI. Aucun
  secret utilisateur.

---

## 8. Sauvegardes & résilience

**Résilience VPS** (reboot / mises à jour imprévues) : `restart: unless-stopped` +
`/data` monté sur le disque hôte → la stack et les données reviennent seules. Fenêtre de
reboot fixée (unattended-upgrades, 4h). Cron health-check qui alerte si la stack n'est pas
remontée.

**Backups** :
- cron nocturne : `tar` de `/data/users/*` + `/data/shared/*` + `accounts.json` →
  `/data/backups/data-AAAAMMJJ.tar.gz.age` (**chiffré**), 14 jours gardés (~10-30 Mo).
- copie hors-VPS chiffrée (release privée GitHub ou stockage objet) → survit à une perte
  du VPS.
- backup automatique **juste avant** les jobs destructeurs (`canonicalize`,
  `merge_corpus`).
- **Procédure de restauration** documentée : `age -d` → `tar -x` dans `/data` →
  `docker compose up -d`.

---

## 9. Risques & parades

| Risque | Parade |
|---|---|
| Stockage des tokens Discogs/Spotify + Subsonic d'autres personnes | chiffrement au repos ; HTTPS ; hash argon2 pour les mots de passe applicatifs |
| Backup contenant plusieurs identités | archives chiffrées (`age`), obligatoire hors-VPS |
| Ban IP / rate-limit Discogs à plusieurs users sur une IP | **non-sujet** : rate-limit par token + OAuth par user + file de jobs consciente du rate-limit + backoff |
| Quota YouTube épuisé | cascade clé appli → clé perso → message ; cache `search` agressif ; hausse de quota Google (formulaire) |
| Contention des jobs | 1 worker, FIFO, round-robin per-user |
| Fuite via un cache « partagé » | liste explicite des fichiers partagés ; tout par-user par défaut |
| Croissance mémoire (`_LOAD_CACHE`) | éviction LRU avec plafond d'octets |
| Migration A→B plus tard | schéma par-user propre et documenté → import JSON→SQL mécanique |
| « Multi-user » ≠ « public » (RGPD, suppression de compte, abus) | rester **sur invitation entre proches** jusqu'à décision explicite |
| Rewrite d'historique / secret committé | `.gitignore` strict, pas de `git add -A`, revue de `git status` avant commit |

---

## 10. Plan d'implémentation par étapes

Chacune = une branche + une PR. `main` protégé (PR + 1 review + CI verte) une fois la CI
en place.

| # | Étape | État |
|---|---|---|
| 0 | Collaboration : `CLAUDE.md`, CI, `data.sample/`, protection de `main`, auto-deploy + rollback | ✅ fait |
| 1 | Dossiers par utilisateur : `users/<uid>/` + `shared/`, migration idempotente | ✅ fait (PR #1) |
| 2 | Comptes : `accounts.json` (scrypt), login identifiant+mdp, cookie signé HMAC, invitations, `/logout` | ✅ fait (PR #2,3,4) |
| 3 | File de jobs : `queue.json`, worker unique round-robin, statuts par utilisateur | ✅ fait (PR #5) |
| 4 | HTTPS + domaine : reverse-proxy Caddy, Let's Encrypt | ⛔ **bloqué** : besoin d'un nom de domaine pointant sur le VPS |
| 5a | YouTube : cache partagé `youtube_cache.json` + cascade de quota (perso → appli) | ✅ fait (PR #6) |
| 5b | OAuth Discogs + Spotify (« Connecter X ») | ⛔ **bloqué** : besoin de l'étape 4 (redirect URI HTTPS) + apps développeur enregistrées |
| 6 | Backups chiffrés : `scripts/backup.sh` (AES-256 openssl), cron 04:00 UTC, rétention 14 | ✅ fait (PR #7) — copie hors-VPS = TODO |
| 7 | Retrait de Streamlit : `crate-radar` hors compose, `crate_radar.py` → `archive/` | ✅ fait |

---

## 11. Ce qui est retiré

- **`crate_radar.py` (Streamlit)** : gelé et mono-locataire, non convertible sans le même
  travail. Retiré à l'étape 7. La couche `crate_jobs.py` reste (partagée, adaptée au
  `uid`).
- **`APP_PASSWORD`** : remplacé par les comptes (étape 2).
