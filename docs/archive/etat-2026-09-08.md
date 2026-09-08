# État courant & notes d'exploitation

Reprise de contexte pour une session neuve (cloud comprise). `CLAUDE.md` = les règles ;
ce fichier = où en est le chantier, ce qui reste à faire, les pièges appris.
Dernière mise à jour : 2026-09-02.

## Cadre

- `radar_web/` est **l'interface active** (VPS `:8600`, `https://radar.hubclaudel.fr`).
  L'ancienne appli Streamlit est gelée dans `archive/` — **ne pas y répercuter les
  évolutions**. Seuls la couche de données JSON et `crate_jobs.py` sont communs.
- Le retour utilisateur vient **au fil de l'eau** pendant qu'il teste. Éviter les gros
  sprints de features autonomes ; livrer par petits lots vérifiables.
- Déploiement : merge sur `main` → GitHub Actions → VPS OVH (`git reset --hard` + rebuild
  Docker + health-check + rollback auto). `main` est protégée : **PR obligatoire**, check
  CI `check` requis, pas de push direct. **Aucune session (cloud ou locale) ne peut faire
  de SSH vers le VPS** — dépannage manuel = l'utilisateur lance la commande avec `!`.
- Dev local : `CRATE_DATA_DIR=$PWD/data RADAR_NO_AUTH=1 python3 -m uvicorn radar_web.app:app --port 8600`.
  Sans `RADAR_NO_AUTH=1` l'appli exige un login même sans compte. Ne jamais poser cette
  variable dans le `.env` du VPS.

## Multi-utilisateur (Option A — `docs/architecture.md`)

Étapes 0 → 7 **faites et déployées** : données `users/<uid>/` + `shared/`, migration
idempotente au démarrage, uid courant via `contextvars`, comptes réels
(`radar/accounts.py`, scrypt stdlib, cookie HMAC `uid.exp.sig`), invites à usage unique,
file d'attente de jobs (`radar/jobs.py` + `radar_web/worker.py`, service `radar-worker`,
round-robin par uid pour protéger le rate limit Discogs partagé), cache YouTube partagé
(`radar/ytcache.py`), backup nocturne chiffré, Streamlit retiré, Caddy + Let's Encrypt.

**Bloqué, besoin utilisateur** : étape 5b = OAuth « Connecter Discogs / Spotify ». Exige
2 apps développeur enregistrées + les redirect URIs sur le domaine HTTPS live. À la
reprise de 5b, l'utilisateur voulait envisager `/model` Opus (design sensible) plutôt que
Sonnet pour l'implémentation OAuth + toute migration de schéma.

## Revue de sécurité (PR #21, mergée + déployée + vérifiée)

Trois bloquants réels, tous corrigés :
- **B1** `load_config()` injectait les secrets `.env` dans **n'importe quel** compte → le
  `/patte` d'un invité affichait le token Discogs / mot de passe Bandcamp du owner en
  clair. Désormais réservé au owner, dans `store.py` **et** `crate_jobs.cfg_load()`.
- **B2** `/logout` ne déconnectait pas (le middleware réémettait le cookie après
  `delete_cookie`). `_guard` saute `/logout`, qui est POST-only.
- **B3** `_dev_mode()` pouvait s'auto-armer (`accounts.json` illisible → appli ouverte sur
  Internet en owner). Exige maintenant `RADAR_NO_AUTH=1` explicite.

Plus : session liée à une tranche du hash de mot de passe (révocation), check `Origin` sur
les mutations, rate limit login (5/60 s), mot de passe ≥ 10 car., invites owner-only +
expiration 48 h + verrou d'écriture, ContextVar uid fail-closed (défaut `None`),
`.gitignore` étendu. **Aucune fuite réelle** — pas de second compte n'a jamais existé,
pas de rotation de secret nécessaire.

**Délibérément non fait : jetons CSRF.** Cookie `SameSite=lax` + toute mutation en POST +
check `Origin` suffisent. Ne pas rétrofitter des jetons sur les appels `hx-post`.

## Nuit du 2026-09-01 → 02 (PRs #22–#25, toutes mergées + déployées)

- **#22 Bandcamp par track** — `radar/bandcamp.py` interroge l'API d'autocomplétion
  publique `bcsearch_public_api/1/autocomplete_elastic` (JSON, sans auth ni Cloudflare,
  ≠ marketplace Discogs). Score tokens artiste+titre ; ne garde un lien direct que si le
  sous-domaine `<x>.bandcamp.com` confirme le compte artiste/label, sinon repli sur l'URL
  de recherche. Route `/bc/go?a=&t=&l=&kind=t|a`. Câblé : pistes de release, tracks de DJ
  sets, lien Bandcamp des cartes de recherche. Cache `shared/bandcamp_cache.json` 30 j.
  **Endpoint non documenté → peut casser ; le repli couvre.**
- **#23 Panier interne** — onglet « 🛒 Panier » dans Mon univers, bouton « ➕ panier » sur
  les cartes (`results.html` → recherche + `/disco`), `users/<uid>/cart.json`, routes
  `/cart` `/cart/add` `/cart/remove`.
- **#24 Catalogue de vendeurs** — `radar/sellers_seed.py` = **141 vendeurs Discogs électro
  européens** (1 username Discogs chacun, compilés par agents de recherche, **non vérifiés
  API**). `radar/sellers.py` : catalogue partagé `shared/sellers_catalog.json`, snapshots
  `shared/seller_inventory/<u>.json`, `cart_coverage()`. Job `scan_catalog`
  (`crate_jobs.py`) : snapshot inventaire « For Sale » par vendeur, diff nouveautés,
  désactive un compte KO 2×. Réglages → « Catalogue de vendeurs ». Panier → « Regrouper
  chez un vendeur » = classement par nb d'articles du panier en stock chez chaque vendeur.
- **#25 UI polish** (lot sûr d'une revue Opus) — media query anti-zoom iOS, viewport +
  autocomplete sur /login et /register, état « occupé » htmx global, `hx-confirm` sur
  retrait label / vidage inbox / suppression règle veille, fix « graines de graphe ne
  cochent pas `mode=seeds` », bannières `?saved=1`, bouton submit réel sur `/patte`.

## À FAIRE — opérationnel (côté utilisateur, non bloquant pour coder)

1. **Lancer le scan vendeurs une fois à la main** depuis Réglages → Catalogue de vendeurs
   (~1 h pour 141 vendeurs). Vérifier que ça termine, regarder la couverture du panier.
2. Ensuite seulement, poser **`RADAR_SELLER_SCAN=1`** dans l'environnement du service
   `radar-worker` (via compose, **pas** dans le `.env` VPS) pour activer le scan hebdo
   automatique (`worker._maybe_weekly_scan`, période 7 j).

## À FAIRE — code (identifié, non commencé)

- **Revue Opus, items « M »** (non faits en #25) : pagination de la table d'artistes
  (250 lignes en cul-de-sac), extraction d'un composant CSS `.tbl` (style recopié dans
  3 partials), multi-selects genre/style en `.seg`, `<label for>` non reliés (~30 champs),
  libellés anglais bruts dans Réglages.
- **Liens `/disco`** depuis les listes de reco + cartes de recherche (suite de PR #20).
- Prix médian réel : **pas dans l'API** — ne pas réessayer (cf. ci-dessous).
- UI de revue des artistes « approx » (27 artistes marqués par la résolution nocturne).
- Suppression par track / par DJ dans « Mes sets ».
- Idée « mode annotation » in-app : décidée = screenshots annotés + ids de section stables
  d'abord, ne construire l'outil que si la friction persiste.

## Pièges appris (ne pas refaire)

- **Marketplace Discogs** (prix livré, liste des annonces, décompte « en vente en
  France ») : **inobtenable**. Pas d'API depuis 2021 ; pages `/sell/release/{id}`
  protégées Cloudflare (403 côté serveur, challenge en boucle même en pilotant le vrai
  Chrome de l'utilisateur) ; le prix de port n'apparaît qu'à un utilisateur **connecté**
  avec adresse enregistrée. Abandonné. On garde : overlay `release_meta` (note ★, « dès
  X € · hors port » depuis `lowest_price`, « N en vente » depuis `num_for_sale`, cache
  24 h) + lien `🇫🇷 voir` vers `discogs.com/sell/release/{id}?ships_from=France`.
- `discogs_get(token, path, params)` **ne lève pas** — renvoie `{}` sur réponse non-ok.
  Tester le dict vide, pas une exception.
- Graphe producteur `producer_graph.edges` est biparti graine↔candidat : pas de données
  candidat↔candidat, donc pas de vrai depth-2 sans nouveaux appels API.
- Ne pas rebuild le conteneur pendant qu'un job tourne (ça le tue).

## Accès

- Public : `https://radar.hubclaudel.fr` (login `owner` / valeur de `APP_PASSWORD` du VPS).
- Admin privé : Tailscale `http://100.94.157.91:8600`.
- Manuel VPS (dépannage) : clé sur le Mac `~/.ssh/radar_vps`, `ubuntu@57.128.180.93`.
- Backups VPS : `/data/backups/data-*.tgz.enc`, cron hôte 04:00 UTC, passphrase
  `~/radar/secrets/backup.pass`. Restauration : `docs/backup.md`.
