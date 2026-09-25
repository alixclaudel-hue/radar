---
name: delegate
description: >
  OBLIGATOIRE ET PRIORITAIRE — la passerelle multi-fournisseurs
  (`scripts/ai_broker.py`) passe AVANT Claude sur toute tâche déléguable ;
  Claude ne prend le relais qu'une fois la passerelle épuisée, jamais l'inverse.
  À lancer SANS attendre de demande explicite et SANS arbitrage préalable dès
  qu'il s'agit de : écrire un premier jet de fonction, de script ou de tests
  unittest ; rédiger un message de commit, un corps de PR ou une note de release
  à partir d'un `git diff` ; lire, résumer ou diagnostiquer un journal, une
  trace Docker ou un dump de plus de 50 lignes ; pré-digérer un document
  volumineux en résumé JSON structuré (mode context) ; localiser un symbole dans
  le dépôt (mode search). Modes : code, test, context, search, diag, summary,
  pr, reasoning, general (tier auto — heavy pour code/tests, fast pour le
  reste). `--json-output` force la sortie JSON sur n'importe quel mode. Cette
  règle n'est pas laissée à la vigilance du modèle : le hook
  `scripts/hooks/delegation_gate.py` BLOQUE le commit et l'écriture d'un test ou
  d'un nouveau module `.py` tant qu'aucun reçu récent n'existe. Un appel qui
  ÉCHOUE (quota, panne) écrit son reçu et débloque légitimement la suite — il
  faut l'avoir tenté, pas l'avoir supposé indisponible.
---

# Skill : delegate

Économiser les tokens Claude en sortant du contexte tout ce qui ne demande pas
de jugement. Claude garde l'architecture, la sécurité et la décision finale ; la
passerelle absorbe le volume et les premiers jets.

Ce skill est **fournisseur-neutre** : il décrit le contrat de
[`scripts/ai_broker.py`](../../../scripts/ai_broker.py:1), qui choisit lui-même
le fournisseur et le modèle (OpenRouter gratuit, Gemini, repli DeepSeek payant).
L'ancien alias [`ask-gemini`](../ask-gemini/SKILL.md:1) renvoie ici.

## L'ordre n'est pas négociable

**La délégation d'abord, Claude ensuite.** Pas « Claude évalue si ça vaut le
coup de déléguer » — déléguer est le défaut, et l'exception doit se justifier par
un épuisement CONSTATÉ (quota, panne, sortie inexploitable), pas supposé.

Le garde-fou [`scripts/hooks/delegation_gate.py`](../../../scripts/hooks/delegation_gate.py:1)
(hook `PreToolUse`, déclaré dans `.claude/settings.json`) applique la règle
mécaniquement : il bloque `git commit`, l'écriture d'un `tests/test_*.py` et la
création d'un nouveau module `.py` tant que le journal de reçus ne porte pas de
reçu du bon mode, de moins d'une heure **et d'au moins 50 caractères de prompt**
(seuil `GATE_MIN_PROMPT_CHARS`, configurable via `RADAR_GEMINI_GATE_MIN_CHARS`).
Ce seuil bloque le gate-gaming : un prompt de 3 caractères ne débloque plus rien.
Les reçus anciens (sans champ `prompt_chars`) et les reçus `status: error`
passent toujours — rétrocompatibilité et porte de sortie quand tous les
fournisseurs sont en panne. Le courtier écrit ce reçu à chaque appel, succès
(`status: ok`) **comme échec** (`status: error`) — d'où la porte de sortie : une
tentative sincère qui rate rend la main à Claude, une tentative jamais faite non.

Le même hook signale, **sans bloquer**, tout reçu récent estampillé `paid` dont
le mode ne justifie pas la dépense (seul `reasoning` la justifie, ou une
escalade tracée) : la ligne est écrite une seule fois dans `telemetry.jsonl`,
sous `kind: delegation_gate`. Un appel payant non justifié n'empêche rien, mais
il se voit.

## Gratuit d'abord, payant sur critère explicite

Le courtier ordonne ses candidats **gratuit d'abord** dans tous les
fournisseurs déclarés, puis n'atteint un modèle payant (DeepSeek) que sur un
critère explicite et sous plafond journalier :

| Situation | Ce que fait le courtier |
|---|---|
| Défaut | Essaie les modèles gratuits (OpenRouter gratuit, Gemini), en parageant ceux en cooldown |
| `--mode reasoning` | Le raisonnement justifie le payant : escalade tolérée, dépense tracée |
| `--escalate` | Déclare une escalade consécutive à un échec **vérifié** (compilation, JSON invalide, test au rouge) ; la raison part dans le reçu |
| `--allow-paid` | Autorise explicitement le payant pour cette invocation ; reste soumis au plafond |
| Plafond dépassé | L'appel payant est refusé (aucune dépense au-delà du plafond du jour) |

Un modèle retiré (404) ou en échec répété pose un cooldown (disjoncteur santé) :
il disparaît des candidats sans casser l'appel. Un refus d'authentification
(401/403, faute de clé) ne pose **pas** de cooldown sur le modèle — la faute est
à la clé, pas au modèle.

Pour auditer le routage sans appeler personne ni écrire de reçu :

```bash
python3 scripts/ai_broker.py --mode code --list-candidates "…"
```

## Les deux tiers, et pourquoi il y en a deux

Deux profils, parce que les quotas gratuits sont inversement proportionnels à la
capacité :

| Tier     | Pour quoi                        | Exemples de modes              |
|----------|----------------------------------|--------------------------------|
| `heavy`  | il faut raisonner                | `code`, `test`, `reasoning`, `pr` |
| `fast`   | du volume, peu de raisonnement   | `context`, `search`, `diag`, `summary`, `general` |

Le tier se déduit du `--mode` : `code`/`test` → `heavy`, tout le reste → `fast`.
Ne le forcer avec `-t` que pour une raison précise (un diff énorme qui mérite du
raisonnement, ou un quota `heavy` déjà à sec).

## ⚠️ Sur le VPS : lance ces commandes depuis `~/radar-work`, jamais `~/radar`

Les exemples ci-dessous passent des chemins **absolus** sous `~/radar-work` pour
tout ce qui est *écrit* (`-o`). Sur le VPS, `~/radar` (checkout prod, lecture
seule) et `~/radar-work` (clone de travail) ont la même arborescence : un chemin
relatif « marche » identiquement dans les deux, sans aucune erreur — mais le reçu
écrit et tout fichier produit (`-o`) atterrissent dans le répertoire réel de la
commande, pas dans celui qu'on croit. Une sortie écrite par mégarde dans
`~/radar/worktrees/…` ou dans le mauvais checkout ne se voit pas. Vérifier `pwd`
en cas de doute, et **préférer un `-o` absolu**. Détail et cas réel (régression de
télémétrie causée par cette même ambiguïté) → `CLAUDE.md` pt 50 et
[`vps-ops/SKILL.md`](../vps-ops/SKILL.md:1).

## Les recettes

### 1. Écrire une fonction ou un script (mode `code`)

```bash
python3 scripts/ai_broker.py \
  --mode code \
  --check-syntax \
  -o /home/ubuntu/radar-work/radar_web/radar/mon_module.py \
  "Écris une fonction qui ... Contraintes : bibliothèque standard seulement, ..."
```

`--check-syntax` refuse d'écrire un fichier qui ne compile pas (code de retour
3 ; argparse garde le 2 pour ses propres erreurs de ligne de commande), et une
réponse vide fait échouer la commande au lieu d'écraser un fichier existant par
du néant. Sans ces deux garde-fous, un aléa d'API détruit du code en silence.

En mode `code`/`test`, la réponse passe par une **enveloppe JSON** : le modèle
rend `{"code": "...", "notes": "..."}`, le courtier écrit le `code` dans `-o`.
Si le code ne compile pas, il **relance** le modèle en lui renvoyant l'erreur de
compilation (`--max-repairs`, défaut 1 ; `0` pour refuser du premier coup).
`--no-envelope` désactive tout ça (écriture brute, sans vérification ni relance).

Quand la réponse mélange un bloc ```python et un bloc ```bash « pour lancer »,
seuls les blocs Python sont gardés : tout concaténer produirait un fichier qui ne
compile pas.

### 2. Générer des tests unitaires (mode `test`)

```bash
python3 scripts/ai_broker.py \
  --mode test \
  --check-syntax \
  -f radar_web/radar/mon_module.py \
  -o /home/ubuntu/radar-work/tests/test_mon_module.py \
  "Génère les tests de la fonction XYZ, cas limites compris."
```

`-f` est répétable : joindre aussi le module voisin ou un test existant donne au
modèle le style de la maison et évite un premier jet à côté des conventions.

### 3. Commit, PR et doc (mode `pr`)

```bash
git diff origin/main | python3 scripts/ai_broker.py --mode pr --stdin
```

Renvoie 4 blocs : TITRE COMMIT, MESSAGE COMMIT, CORPS DE PR, MAJ CLAUDE.md. Le
prompt embarque les conventions du dépôt (français, impératif, pas de
`feat:`/`fix(scope):`) — relire quand même le titre avant de committer.

### 4. Diagnostiquer un incident (mode `diag`)

```bash
docker logs radar-web --tail 200 | python3 scripts/ai_broker.py --mode diag --stdin
```

Réponse en 3 points : ORIGINE, CAUSE RACINE, PISTE DE FIX. Pour compresser sans
diagnostiquer (relevé de compteurs, balayage d'un journal sain), préférer
`--mode summary`.

### 5. Pré-digérer des documents (mode `context`)

```bash
python3 scripts/ai_broker.py --mode context \
  -f scripts/ai_broker.py -f CLAUDE.md -o /tmp/digest.json
```

Produit un JSON structuré (`title`, `sections`, `key_points`, `todos`,
`dependencies`, `uncertain`) — le flag `--json-output` est **implicite** en mode
`context`. Contrairement à l'ancien mode `read`, `context` **pave plusieurs
documents en un seul appel** et **met les résumés en cache disque** : un document
déjà analysé n'est pas renvoyé au modèle. `--no-cache` force à tout réanalyser
(quand on se méfie d'une entrée de cache).

Usage type : avant de lire un script, un `CLAUDE.md` ou un skill volumineux —
**y compris un document d'architecture ou de sécurité, sans exception** —
déléguer la lecture et exploiter le résumé structuré plutôt que de charger le
fichier entier dans le contexte Claude.

`uncertain` (liste de `{location, question}`) est le mécanisme qui rend ça sûr
sur un document sensible : quand le modèle n'est pas certain d'un passage, il le
signale au lieu de trancher en silence. Les fichiers injectés via `-f` sont
numérotés, donc `location` est en général un numéro de ligne exact — Claude
rouvre alors CET endroit précis, jamais le document entier par défaut. Une liste
`uncertain` vide est normale sur un document trivial ; sur un document
d'architecture ou de sécurité, une liste vide à répétition mérite un coup d'œil.

### 6. Localiser un symbole dans le dépôt (mode `search`)

```bash
python3 scripts/ai_broker.py --mode search \
  --root scripts --root radar_web \
  "où est définie la fonction qui écrit un reçu de télémétrie ?"
```

La recherche locale se fait **sans appel** quand elle est sans ambiguïté. Si
plusieurs fichiers correspondent, le courtier contraint le modèle à choisir dans
une **liste fermée** de candidats ; un chemin hors liste fait sortir en code 4.
`--max-hits` borne le nombre de correspondances gardées.

### 7. Raisonner (mode `reasoning`, payant justifié)

```bash
python3 scripts/ai_broker.py --mode reasoning --effort high "…"
```

À réserver aux cas où aucun modèle gratuit ne suffit et où le raisonnement
justifie la dépense : c'est le seul mode qui légitime un appel payant aux yeux du
signal de télémétrie. `--effort low|medium|high` traduit le budget de réflexion
dans le champ propre au fournisseur (thinking budget Gemini, paramètre natif
OpenAI-compatible), et reste sans effet sur un modèle dont la capacité n'est pas
déclarée au catalogue.

## La garantie JSON

En mode `context`/`search` (et partout avec `--json-output`), la sortie est du
JSON **garanti** par une échelle de repli, pas par la bonne volonté du modèle :

1. **sortie structurée native** — quand le modèle déclare la capacité, le schéma
   est envoyé au fournisseur (`response_format` / `json_schema`) et le format est
   imposé côté API ;
2. sinon, **mode JSON générique** (`{"type": "json_object"}`) quand c'est accepté ;
3. **validation** locale du JSON reçu ; s'il est invalide, **un essai de
   réparation** relance le modèle en lui rendant l'erreur ;
4. si rien n'aboutit : **échec explicite**, jamais un JSON silencieusement faux.

Règle de conception : on ne déclare jamais un champ que le catalogue n'annonce
pas. Le courtier est permissif quand une capacité est *absente* (il tente) et
strict quand elle est *déclarée* (un schéma non déclaré n'est pas envoyé).

## Quand NE PAS déléguer

- **Le code qui part en commit.** La passerelle dégrossit, Claude relit, corrige
  et assume. Ne jamais coller une sortie telle quelle dans le dépôt sans l'avoir
  lue — `--check-syntax` prouve que ça compile, pas que c'est juste.
- **Le scoring et les décisions produit.** Un premier jet sur `scoring.py`
  coûte plus cher à auditer qu'à écrire.
- **Tout contenu sensible.** Jamais de token, de clé, de contenu de `.env` ni de
  donnée personnelle dans un prompt : c'est un service externe, hors du périmètre
  de confiance du dépôt. Le masquage `redact.py` protège les motifs connus, mais
  il ne dispense pas de filtrer un `docker logs` ou un dump avant de le piper.
- **Une tâche déjà tenue en contexte.** Déléguer 20 lignes déjà lues coûte un
  aller-retour pour rien.

## Ajouter ou changer un modèle : le catalogue, pas le code

Les modèles et les fournisseurs ne sont **pas** écrits en dur dans le courtier :
ils viennent de [`config/ai_models.json`](../../../config/ai_models.json:1),
engendré par
[`scripts/ai/refresh_models.py`](../../../scripts/ai/refresh_models.py:1). Pour
changer un modèle, régénérer le catalogue ; un nom absent du catalogue répond 404
et le disjoncteur santé l'écarte ensuite tout seul.

```bash
python3 scripts/ai/refresh_models.py
python3 scripts/ai_broker.py --mode code --list-candidates "ping"
```

Le second commandement montre, sans dépense, quels candidats retiennent leur
place et pourquoi les autres sont écartés.

## Repères de code

- Point d'entrée : [`scripts/ai_broker.py`](../../../scripts/ai_broker.py:1) —
  contrat CLI identique à l'ancien `ai_query.py`, plus les leviers
  multi-fournisseurs (`--allow-paid`, `--escalate`, `--effort`, `--provider`,
  `--list-candidates`, `--source`).
- Adaptateurs : [`scripts/ai/providers.py`](../../../scripts/ai/providers.py:1)
  (OpenAI-compatible et Gemini natif).
- Routage : [`scripts/ai/router.py`](../../../scripts/ai/router.py:1).
  Quota et rotation de clés : [`scripts/ai/quota.py`](../../../scripts/ai/quota.py:1).
  Disjoncteur : [`scripts/ai/health.py`](../../../scripts/ai/health.py:1).
  Masquage : [`scripts/ai/redact.py`](../../../scripts/ai/redact.py:1).
- Banc hors ligne : [`scripts/bench_ai_broker.py`](../../../scripts/bench_ai_broker.py:1).
- Ancien transport Gemini seul, conservé et testé :
  [`scripts/ai_query.py`](../../../scripts/ai_query.py:1) — ne plus l'appeler
  directement pour du travail multi-fournisseurs.
