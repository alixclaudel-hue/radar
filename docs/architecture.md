# Architecture — Radar multi-utilisateur (Option A)

Référence pour terminer le chantier multi-utilisateur. Étapes 0-7 **faites et
déployées** (dossiers par utilisateur, comptes, file de jobs, backups chiffrés,
Streamlit retiré — détail dans `CLAUDE.md`). Bloquées : étape 4 (HTTPS +
domaine) et 5b (OAuth Discogs/Spotify), ci-dessous. Historique complet des étapes
faites → `docs/archive/etat-2026-09-08.md`.

Cible : 3 à ~20 utilisateurs sur invitation, fichiers JSON namespacés
`/data/users/<uid>/…` + `/data/shared/`. Au-delà → migration base SQL (Option B), pas
avant.

---

## 1. Étapes restantes

| # | Étape | État |
|---|---|---|
| 4 | HTTPS + domaine : reverse-proxy Caddy, Let's Encrypt | ⛔ **bloqué** : besoin d'un nom de domaine pointant sur le VPS |
| 5b | OAuth Discogs + Spotify (« Connecter X ») | ⛔ **bloqué** : requiert l'étape 4 (redirect URI HTTPS) + apps développeur enregistrées |

---

## 2. Modèle de données — partagé vs par-utilisateur

| Fichier | Portée | Raison |
|---|---|---|
| `crate_radar_config.json` (labels, taste_categories, artist_categories, veille_rules, connexions) | **par-user** | définit goût / config utilisateur |
| `taste_corpus.json` | **par-user** | tracks ingérées (YouTube/Spotify/Bandcamp/DJ sets) de l'utilisateur |
| `labels_profile.json` | **par-user** | profils de styles des labels de sa base |
| `labels_resolved.json`, `artists_resolved.json` | **par-user** | résolutions liées à sa base |
| `producer_graph.json` | **par-user** | graphe construit sur ses graines |
| `collection_cache.json` | **par-user** | collection + wantlist Discogs |
| `reco_feedback.json`, `scoring_profiles.json` | **par-user** | apprentissage propre |
| `radar_web_searches.json` | **par-user** | historique de recherche |
| `pending_enrich.json`, `veille_new.json`, `veille_seen.json`, `sellers_seen.json` | **par-user** | files / états de veille |
| `lookup_cache.json` (release/artiste → label/style Discogs) | **partagé** | métadonnées Discogs publiques, neutres — résolution d'un user sert à tous |
| `release_meta_cache.json` (note ★, prix, num_for_sale) | **partagé** | données de release publiques |
| `youtube_cache.json` (résultats `search`, résolutions ▶, métadonnées vidéo) | **partagé** | données YouTube publiques ; économise le quota |

**Règle :** un fichier est « partagé » seulement s'il ne contient **rien** de spécifique
à un utilisateur. Toute nouveauté est par-user par défaut ; partage seulement après audit.

---

## 3. Connexions aux services externes

Chaque utilisateur connecte ses propres comptes. Stocké dans sa config, **chiffré au
repos** (clé dans le `.env` du VPS, hors git).

| Service | Méthode | UX | Notes |
|---|---|---|---|
| **Discogs** | **OAuth 1.0a** (« Connecter Discogs ») | 1 clic | token+secret sans expiration. Rate-limit **par token** → 60/min/utilisateur, IP partagée du VPS non problématique. Collage de *personal token* gardé en secours. |
| **Spotify** | **OAuth 2.0** (« Connecter Spotify ») | 1 clic | débloque toute la bibliothèque (playlists privées, likes, top artistes). Dev-mode plafonné à 25 users, extension = formulaire gratuit. |
| **YouTube** | **1 clé API de l'appli** (`YOUTUBE_API_KEY` dans `.env`) | rien à faire | lecture seule de données publiques (playlists collées, recherche DJ sets, résolution bouton ▶). **Cascade quota** : `403 quotaExceeded` sur clé appli → bascule clé perso de l'user si renseignée → sinon message + champ pour en coller une. Préférer clé perso si dispo (protège le pot commun). OAuth Google seulement si besoin futur de playlists privées (scope restreint → audit Google au-delà de 100 users). |
| **Bandcamp** | **mot de passe d'app Subsonic** (identifiant + mot de passe généré, collés) | 1 collage unique | pas d'OAuth ni d'API publique. Subsonic = mot de passe **à portée limitée** (musique achetée uniquement). Chiffré au repos. Incontournable. |

**Prérequis OAuth : HTTPS + nom de domaine.** Les callbacks (`https://domaine/oauth/callback/<service>`)
ne peuvent pas pointer sur une IP Tailscale en clair. → étape bloquante, priorité du plan.

---

## 4. Sécurité

- **HTTPS + domaine** : reverse-proxy (Caddy ou Traefik) devant `radar-web`, certificat
  Let's Encrypt auto. Prérequis des OAuth + hygiène de base (actuellement « Non sécurisé »
  en clair). Accès Tailscale conservable en plus pour l'admin.
- **Secrets au repos** : tokens OAuth + mot de passe Subsonic chiffrés (clé symétrique
  `age`/Fernet dans `.env`, jamais dans git). `accounts.json` = hash argon2id, pas de
  mot de passe en clair.
- **`.gitignore`** couvre `/data/`, `.env`, tous les `*.json` de données, `discogs_state.json`,
  `discogs_profile/`. `git add -A` proscrit — ajouter les fichiers nommément.
- **`.env` VPS** : `APP_SESSION_SECRET`, `DATA_ENCRYPTION_KEY`, `YOUTUBE_API_KEY`,
  identifiants OAuth de l'appli (Discogs/Spotify), `VPS_*` pour déploiement CI. Aucun
  secret utilisateur.

---

## 5. Risques restants (liés aux étapes 4/5b)

| Risque | Parade |
|---|---|
| Stockage des tokens Discogs/Spotify d'autres personnes | chiffrement au repos ; HTTPS ; hash argon2 pour mots de passe applicatifs |
| « Multi-user » ≠ « public » (RGPD, suppression de compte, abus) | rester **sur invitation entre proches** jusqu'à décision explicite |

Backups, résilience VPS et gestion des risques déjà traités → `docs/backup.md` et
`CLAUDE.md`.