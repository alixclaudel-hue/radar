# Brief — télémétrie et diagnostic (`radar_ops`)

État au 23/09. Lot 1 livré et vérifié (commit `aee54db`), lot 2 amorcé, lot 3 à faire.
Ce document est la référence du chantier : il porte les décisions utilisateur déjà
prises et les points encore ouverts.

## Décisions utilisateur actées

1. **Hébergement** : service Docker séparé `radar-ops`, port 8610, même image et même
   volume que `radar-web`, jamais exposé par Caddy. Un outil de diagnostic qui meurt
   avec l'application qu'il surveille ne sert à rien au moment où on en a besoin.
2. **Transport de la télémétrie** : git seulement. Pas d'endpoint HTTP d'ingestion —
   il faudrait ouvrir un port en écriture, provisionner un secret dans chaque session
   et élargir la politique réseau, pour gagner quelques dizaines de secondes.
3. **Mesure des jetons** : jetons Gemini réellement consommés (`usageMetadata`),
   uniquement. Aucune estimation d'économie côté Claude : l'économie est un
   contrefactuel, la publier comme une mesure serait malhonnête.
4. **État du scoring** : fraîcheur des scores précalculés, par comparaison d'empreinte
   entre le moment du calcul et l'instant présent.

## Ce qui est livré (lot 1)

- `radar_ops/` — quatre pages : jobs et file d'attente, inventaire des bases,
  fraîcheur du scoring, journal des actions. Temps réel par SSE sur les deux flux
  qui bougent (jobs, actions).
- **Débit et temps restant** (`radar_ops/sampler.py`) : le fichier de statut d'un job
  n'est qu'un instantané `(done, total)`, sans historique — aucun débit n'y est
  lisible. L'échantillonneur garde une fenêtre glissante de 10 minutes par job et en
  dérive la cadence, affichée en « Débit /min » et « Restant ». Un job à l'arrêt
  n'affiche pas de temps restant plutôt qu'un temps faux. Un compteur qui recule
  (relance du même job) purge l'historique, sans quoi le débit deviendrait négatif.
- **Journal des actions de l'utilisateur** (`radar_web/radar/opslog.py`) : écrit depuis
  `_guard` dans `radar_web/app.py`, et pas depuis un second middleware — Starlette
  exécute `call_next` dans une tâche au contexte distinct, l'uid n'y serait pas
  garanti. Méthode, chemin, code de retour, durée ; jamais la chaîne de requête ni le
  corps, qui peuvent porter un mot de passe ou un jeton Discogs. Rotation à 5 Mo.
- **Fraîcheur du scoring** : `job_scorestore_releases` estampille la base
  (`scorestore_meta`). La page ne dit pas seulement « périmé », elle nomme ce qui a
  changé : « la configuration de scoring a changé », « producer_graph.json a été
  réécrit ».
- **Authentification factorisée** (`radar_web/radar/websession.py`), partagée par les
  deux applications.

## Lot 2 — télémétrie du développement

### Livré

- `scripts/hooks/telemetry.py` — capture des événements de session. Ne journalise
  jamais une commande `Bash`, seulement sa `description` : c'est la condition pour que
  ce journal ait le droit de quitter la machine.
- `scripts/telemetry_ship.py` — expédition vers la branche `telemetry` par la
  plomberie git, sans toucher à l'index ni à la branche courante. Commit sans parent
  poussé en force : la branche est une boîte aux lettres, pas un historique.
- Instrumentation de `scripts/ai_query.py` — le reçu de chaque appel porte désormais
  le modèle réellement utilisé, le tier, le nombre de replis de cascade, les jetons
  rapportés par `usageMetadata` et la durée. Vérifiée par un appel réel. Le hook
  `gemini_gate.py` ne lit toujours que `ts`, `mode` et `status` : un reçu ancien,
  sans ces champs, reste valide.
- `RADAR_TELEMETRY_DIR` : redirige l'écriture du journal hors du dépôt. Sur le VPS la
  variable pointera `/data/ops/`, ce qui évite de salir le checkout de production —
  et aucune expédition git n'y est nécessaire, `radar_ops` tourne sur la même machine
  et lit le fichier sur place.
- Tests (`tests/test_telemetry.py`) des deux scripts, dont la règle de sécurité
  (`Bash` journalisé par sa description, jamais par sa commande) et la propriété de
  boîte aux lettres de la branche `telemetry` (commit sans parent, index intact).

- `scripts/telemetry_pull.py` — réception sur le VPS de ce que les sessions **hors**
  VPS ont expédié : `git fetch` d'une référence dédiée (jamais un `pull`, la branche
  est réécrite à chaque envoi), puis fusion dans `<data>/ops/` des seules lignes dont
  le `ts` dépasse le maximum déjà stocké. Les expéditions se recouvrent largement —
  chacune renvoie les 2000 dernières lignes — et sans ce filtre le fichier local se
  peuplerait de doublons. Écrit en Python et non en shell comme prévu initialement :
  la fusion suppose de lire le `ts` de chaque ligne, ce qui en shell imposerait `jq`,
  absent du VPS. Écriture par fichier temporaire puis `os.replace`, parce que
  `radar_ops` lit ce fichier pendant qu'on y écrit.
- Page **Délégation** de `radar_ops` (`radar_ops/delegation.py`, route `/delegation`) :
  totaux, répartition par mode et par modèle, volume par jour sur une fenêtre bornée,
  part déléguée, et une ligne par session de développement. Ajoutée au balayage de
  routes de la CI. Une ligne ancienne, sans les champs récents, compte pour 0 plutôt
  que d'être écartée — sinon le tableau de bord ignorerait tout l'historique antérieur
  à l'instrumentation.

- Branchement des hooks dans `.claude/settings.json` : `SessionStart`,
  `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`. La capture tourne donc dans
  toute session Claude Code sur ce dépôt, celle du VPS comprise. L'expédition, elle,
  n'est PAS branchée sur un hook : pousser une branche à chaque fin de tour coûterait
  un aller-retour réseau par tour, pour une donnée qu'on ne lit pas à cette cadence.

### À faire

- Skill `telemetry` d'orchestration (cf. section suivante) : enchaîner expédition,
  réception et interprétation par Gemini.
- Vérifications en conditions réelles sur le VPS (cf. `CLAUDE.md`, TODO).

### Skill `telemetry` — qui fait quoi

Demande utilisateur : le processus de traçabilité doit être porté par un skill dédié,
Claude intervenant le moins possible, et le skill doit être invocable par la session
VPS.

Accord sur l'intention : **Claude n'a rien à faire dans cette boucle**, recopier des
événements n'est pas un travail de modèle. Réserve, à trancher par l'utilisateur :
Gemini non plus. Du transport de données est un travail de **code** — déterministe,
instantané, gratuit, incapable d'altérer un chiffre. Mettre un modèle de langage dans
un chemin de recopie y introduit de la latence, de la consommation de quota et un
risque de corruption portant précisément sur les chiffres censés être fiables.

Répartition proposée :

| Couche | Exécutant | Justification |
|---|---|---|
| Capture | hook `telemetry.py` | déclenché par le harnais, aucun modèle impliqué |
| Transport | `telemetry_ship.py`, ou lecture directe sur le VPS | déterministe et vérifiable |
| Interprétation | Gemini via `ai_query.py` (`summary`, `diag`) | résumer une session, qualifier les décisions, rédiger le bilan : c'est de la synthèse |
| Orchestration | skill `telemetry` | enchaîne les trois, invocable depuis la session cloud comme depuis la session VPS |

Claude n'intervient que pour relire ce que Gemini produit avant publication — règle du
projet, inchangée.

**Confidentialité, tranchée le 23/09.** Décision utilisateur : `head`, les 200 premiers
caractères de chaque demande (valeur par défaut du hook, aucune variable à poser). Les
autres valeurs restent disponibles sans changement de code — `RADAR_TELEMETRY_PROMPTS`
vaut `none` pour ne garder que la longueur, `full` pour le texte entier. Les étiquettes
d'outils sont gardées telles quelles (chemins relatifs au dépôt, motifs de recherche) :
ce sont des chemins déjà publics dans le dépôt, et c'est ce qui rend la colonne
« outils les plus utilisés » lisible. La commande d'un `Bash` n'est jamais journalisée,
quelle que soit la politique.

## Lot 3 — schémas de flux

Non commencé. Attendu : présentation schématique des flux d'information, produits
d'entrée et de sortie par tâche, boucles de contrôle, tests. Outillage disponible dans
le dépôt : skills `diagram-brief` et `brief-to-excalidraw`.
