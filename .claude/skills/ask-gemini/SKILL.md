---
name: ask-gemini
description: >
  OBLIGATOIRE ET PRIORITAIRE — Gemini (gratuit, via `scripts/ai_query.py`)
  passe AVANT Claude sur toute tâche déléguable ; Claude ne prend le relais
  qu'une fois Gemini épuisé, jamais l'inverse. À lancer SANS attendre de
  demande explicite et SANS arbitrage préalable dès qu'il s'agit de : écrire un
  premier jet de fonction, de script ou de tests unittest ; rédiger un message
  de commit, un corps de PR ou une note de release à partir d'un `git diff` ;
  lire, résumer ou diagnostiquer un journal, une trace Docker ou un dump de
  plus de 50 lignes. Modes : code, test, diag, summary, pr (tier auto — heavy
  pour code/tests, fast pour le reste). Cette règle n'est pas laissée à la
  vigilance du modèle : le hook `scripts/hooks/gemini_gate.py` BLOQUE le commit
  et l'écriture d'un test ou d'un nouveau module `.py` tant qu'aucun reçu
  Gemini récent n'existe. Un appel Gemini qui ÉCHOUE (quota, panne) écrit son
  reçu et débloque légitimement la suite — il faut l'avoir tenté, pas l'avoir
  supposé indisponible.
---

# Skill : ask-gemini

Économiser les tokens Claude en sortant du contexte tout ce qui ne demande pas
de jugement. Claude garde l'architecture, la sécurité et la décision finale ;
Gemini absorbe le volume et les premiers jets.

## L'ordre n'est pas négociable

**Gemini d'abord, Claude ensuite.** Pas « Claude évalue si ça vaut le coup de
déléguer » — la délégation est le défaut, et l'exception doit se justifier par
un épuisement CONSTATÉ (quota, panne, sortie inexploitable), pas supposé.

Le garde-fou `scripts/hooks/gemini_gate.py` (hook `PreToolUse`, déclaré dans
`.claude/settings.json`) applique la règle mécaniquement : il bloque `git
commit`, l'écriture d'un `tests/test_*.py` et la création d'un nouveau module
`.py` tant que `.claude/gemini-receipts.jsonl` ne porte pas de reçu du bon mode
de moins d'une heure. `ai_query.py` écrit ce reçu à chaque appel, succès
(`status: ok`) **comme échec** (`status: error`) — d'où la porte de sortie :
une tentative sincère qui rate rend la main à Claude, une tentative jamais
faite non. Couverture verrouillée par `tests/test_gemini_gate.py`.

## Les deux tiers, et pourquoi il y en a deux

`scripts/ai_query.py` descend une **cascade** de modèles et bascule tout seul
sur le suivant quand le courant est inutilisable (quota épuisé, modèle retiré,
surcharge). Deux cascades, parce que les quotas gratuits sont inversement
proportionnels à la capacité :

| Tier     | Tête de cascade         | Quota | Pour quoi                       |
|----------|-------------------------|-------|---------------------------------|
| `heavy`  | `gemini-3.8-flash`      | étroit| code, tests — il faut raisonner |
| `fast`   | `gemini-3.5-flash-lite` | large | logs, diffs, docs — du volume   |

Le tier se déduit du `--mode` : `code`/`test` → `heavy`, tout le reste →
`fast`. Ne le forcer avec `-t` que pour une raison précise (par exemple un diff
énorme qui mérite du raisonnement, ou un quota `heavy` déjà à sec).

## Les 4 recettes

### 1. Écrire une fonction ou un script (mode `code`)

```bash
python3 scripts/ai_query.py \
  --mode code \
  --check-syntax \
  -o radar_web/radar/mon_module.py \
  "Écris une fonction qui ... Contraintes : bibliothèque standard seulement, ..."
```

`--check-syntax` refuse d'écrire un fichier qui ne compile pas (code de retour
3 ; argparse garde le 2 pour ses propres erreurs de ligne de commande), et une
réponse vide fait échouer la commande au lieu d'écraser un fichier existant par
du néant. Sans ces deux garde-fous, un aléa d'API détruit du code en silence.

Quand la réponse mélange un bloc ```python et un bloc ```bash « pour lancer »,
seuls les blocs Python sont gardés : tout concaténer produirait un fichier qui
ne compile pas.

### 2. Générer des tests unitaires (mode `test`)

```bash
python3 scripts/ai_query.py \
  --mode test \
  --check-syntax \
  -f radar_web/radar/mon_module.py \
  -o tests/test_mon_module.py \
  "Génère les tests de la fonction XYZ, cas limites compris."
```

`-f` est répétable : joindre aussi le module voisin ou un test existant donne
au modèle le style de la maison et évite un premier jet à côté des conventions.

### 3. Commit, PR et doc (mode `pr`)

```bash
git diff origin/main | python3 scripts/ai_query.py --mode pr --stdin
```

Renvoie 4 blocs : TITRE COMMIT, MESSAGE COMMIT, CORPS DE PR, MAJ CLAUDE.md.
Le prompt embarque les conventions du dépôt (français, impératif, pas de
`feat:`/`fix(scope):`) — relire quand même le titre avant de committer.

### 4. Diagnostiquer un incident (mode `diag`)

```bash
docker logs radar-web --tail 200 | python3 scripts/ai_query.py --mode diag --stdin
```

Réponse en 3 points : ORIGINE, CAUSE RACINE, PISTE DE FIX. Pour compresser
sans diagnostiquer (relevé de compteurs, balayage d'un journal sain), préférer
`--mode summary`.

## Quand NE PAS déléguer

- **Le code qui part en commit.** Gemini dégrossit, Claude relit, corrige et
  assume. Ne jamais coller une sortie Gemini telle quelle dans le dépôt sans
  l'avoir lue — `--check-syntax` prouve que ça compile, pas que c'est juste.
- **Le scoring et les décisions produit.** Un premier jet sur `scoring.py`
  coûte plus cher à auditer qu'à écrire.
- **Tout contenu sensible.** Jamais de token, de clé, de contenu de `.env` ni
  de donnée personnelle dans un prompt : c'est un service externe, hors du
  périmètre de confiance du dépôt. Attention aux `docker logs` et aux dumps,
  qui peuvent en contenir — les filtrer avant de les piper.
- **Une tâche déjà tenue en contexte.** Déléguer 20 lignes déjà lues coûte
  un aller-retour pour rien.

## Prérequis : deux modes d'authentification

`scripts/ai_query.py` gère les deux sans configuration :

- **VPS / local** : variable `GEMINI_API_KEY` (clé gratuite sur
  https://aistudio.google.com/apikey). Le script l'ajoute en `?key=` dans l'URL.
- **Session cloud** : un « identifiant » (credential) configuré côté réglages
  de l'environnement (section « Identifiants API », pas « Variables
  d'environnement » — cette dernière est en clair et déconseillée pour un
  secret). Concrètement : type Bearer, en-tête personnalisé renommé
  `x-goog-api-key` (préfixe vide, valeur = la clé), site autorisé
  `generativelanguage.googleapis.com`. Le proxy réseau de la session injecte
  l'en-tête sans jamais exposer la clé au code — `GEMINI_API_KEY` reste absente
  de l'environnement, c'est normal et attendu dans ce mode (vérifié en
  conditions réelles les 20 et 21/09/2026).

C'est pour ce second mode que le script **n'exige jamais** de clé locale :
lever une erreur quand `GEMINI_API_KEY` est absente couperait la session cloud
de sa seule voie d'accès. Un test de non-régression le verrouille
(`tests/test_ai_query.py`).

Si aucun des deux n'est configuré, l'appel échoue avec un message Gemini
explicite (400/401/403) — le dire à l'utilisateur et continuer la tâche sans
délégation plutôt qu'échouer en silence. Si l'erreur est *réseau* et non
d'authentification : depuis la session cloud le réseau sortant est restreint
(CLAUDE.md pt 4), vérifier que `generativelanguage.googleapis.com` est bien
autorisé (mode Custom) avant de conclure à un bug du script.

## Ajouter ou changer un modèle dans une cascade

Les noms doivent exister tels quels dans `ListModels` (v1beta) — un nom
approximatif répond 404, pas une erreur parlante. Vérifier avant d'éditer
`TIER_CASCADES` :

```bash
python3 -c "import json,urllib.request; \
print('\n'.join(sorted(m['name'].replace('models/','') \
for m in json.load(urllib.request.urlopen( \
'https://generativelanguage.googleapis.com/v1beta/models?pageSize=200'))['models'])))"
```

La cascade enjambe un 404, donc un modèle mort ne casse rien — mais il coûte un
aller-retour inutile à chaque appel.
