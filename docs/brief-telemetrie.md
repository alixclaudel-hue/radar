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
- Instrumentation de `scripts/ai_query.py` — écrite et vérifiée par un appel réel,
  consignée dans `docs/lot2-instrumentation-ai_query.md` faute d'avoir pu être
  committée.

### À faire

- **Session VPS incluse dans la télémétrie** (demande utilisateur du 23/09).
  `.claude/settings.json` étant versionné, le hook se déclenche déjà dans toute
  session Claude Code sur ce dépôt, celle du VPS comprise. Deux ajustements :
  - variable `RADAR_TELEMETRY_DIR` pour rediriger l'écriture vers `/data/ops/` sur le
    VPS, plutôt que de salir le checkout de production ;
  - aucune expédition git depuis le VPS : `radar_ops` tourne sur la même machine et
    lit le fichier sur place. Latence nulle, là où le besoin de temps réel est le
    plus fort.
- Récupérateur `scripts/telemetry_pull.sh` pour les sessions **hors** VPS : `git fetch
  origin telemetry` depuis un clone séparé, puis report dans `/data/ops/` des seuls
  événements dont l'horodatage dépasse le maximum déjà stocké.
- Page tableau de bord de délégation dans `radar_ops` : jetons par mode, par modèle,
  taux de repli de cascade, part des tâches déléguées, durée des appels.
- Tests de `scripts/hooks/telemetry.py` et `scripts/telemetry_ship.py`.
- `.claude/telemetry.jsonl` dans `.gitignore`.
- Branchement des hooks dans `.claude/settings.json` : `SessionStart`,
  `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`.

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

**Confidentialité, décision en attente.** Le hook enregistre par défaut les 200
premiers caractères de chaque demande (`RADAR_TELEMETRY_PROMPTS=head`), et ce journal
part sur une branche GitHub. Les autres valeurs sont `none` (longueur seule) et `full`.

## Lot 3 — schémas de flux

Non commencé. Attendu : présentation schématique des flux d'information, produits
d'entrée et de sortie par tâche, boucles de contrôle, tests. Outillage disponible dans
le dépôt : skills `diagram-brief` et `brief-to-excalidraw`.
