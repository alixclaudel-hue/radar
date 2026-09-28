# Diagnostic — Pourquoi Claude ne sous-traite pas (assez) au courtier

**Date** : 2026-09-28
**Auteur** : revue de process (mode Claude)
**Destinataire** : agent code chargé de corriger les points bloquants
**Périmètre** : skill `workflow`, `scripts/ai_broker.py`, `scripts/ai_worker.py`,
`scripts/ai_query.py`, hooks `delegation_gate.py` / `workflow_reminder.py`,
`config/ai_models.json`, `CLAUDE.md`.

---

## 0. Objectif visé par l'utilisateur (rappel non négociable)

Le « chef de projet » (Claude, fil principal) doit :

1. **Cadrer** la demande (une phrase, critère de fin, périmètre exclu).
2. **Découper en WBS** : tâches élémentaires, à périmètre fermé, **par LIVRABLE**
   (jamais par commande shell).
3. **Ordonnancer** : tâches indépendantes lancées **en parallèle**.
4. **Sous-traiter chaque tâche** à un **agent autonome** qui produit le livrable
   (ouvrier `ai_worker.py`, ou appel courtier `ai_broker.py`, ou `explore-leger`
   en lecture seule).
5. **Assembler** les livrables et faire un **contrôle par échantillonnage**
   (relire seulement le douteux, lancer la vérif finale, commit/PR).

**Constat de l'utilisateur** : ce n'est pas ce qui se passe. Le taux de
sous-traitance est trop faible, et surtout **rien ne force mécaniquement** le
respect de ce déroulé.

---

## 1. Résumé exécutif (TL;DR)

Le dispositif de délégation existe et **fonctionne partiellement** : le courtier
route bien vers OpenRouter gratuit, les reçus sont écrits, le gate bloque
quelques actions mécaniques. Mais **le cœur du problème est que le gate ne
couvre que des déclencheurs mécaniques étroits** (commit, nouveau `.py`, tests,
`git diff` brut, `Read` large, `Grep`) et **ne force jamais** :

- le passage par le **WBS** ;
- l'usage de l'**ouvrier `ai_worker.py`** (utilisé **1 fois sur 1252 reçus**) ;
- l'usage de l'**agent `explore-leger`** ;
- la **parallélisation** des tâches indépendantes.

Autrement dit : **la règle « déléguer d'abord » est une consigne de prompt, pas
une contrainte exécutable**. Claude peut — et le fait — produire du code, du
raisonnement et des corrections directement dans le fil principal sans qu'aucun
hook ne s'y oppose, dès lors qu'il n'écrit pas un *nouveau* fichier `.py` et ne
commite pas.

**Preuve chiffrée** (reçus `/home/ubuntu/radar/data/ops/gemini-receipts.jsonl`,
1252 lignes) :

| Indicateur | Valeur | Lecture |
|---|---|---|
| Reçus `mode=worker` (ouvrier) | **1** | `ai_worker.py` quasi jamais utilisé |
| Reçus sans `provider` (appel direct `ai_query.py`, hors broker) | **1094 / 1252 (87 %)** | le broker est contourné dans l'immense majorité de l'historique |
| Reçus avec `provider` (passés par le broker) | 158 (13 %) | — |
| Worktrees présents | **0** (dossier `.worktrees/` vide) | aucun ouvrier en vol |
| Reçus `mode=code` | 480 | production de code massive… mais par quel chemin ? |

> **Nuance importante** : depuis le 25/09, la tendance s'est **inversée** —
> OpenRouter (88) devance Gemini (67) et les appels directs tombent à 25. Sur
> les dernières 24 h : OpenRouter 57, Gemini 44, direct 13. Le courtier est donc
> **réparé récemment**. Le problème résiduel n'est plus le *routage* mais
> l'**absence de contrainte sur le pattern WBS/ouvrier/parallèle**.

**Second constat (demande utilisateur)** : l'écosystème compte **trop de
scripts**, dont plusieurs **morts** (`gemini_gate.py`, `gemini_gateway.py`,
`cloud-gemini-gateway.sh`, `bench_ai_query.py`, skill `ask-gemini`) et un
**vocabulaire « Gemini » omniprésent** alors que Gemini n'est plus qu'un
provider parmi d'autres. Ce désordre **alimente directement** le faible taux de
sous-traitance (mauvais point d'entrée recopié). Voir §8.

---

## 2. Architecture réelle (pour situer les correctifs)

```
Claude (fil principal)
   │
   ├─ hook UserPromptSubmit ──► workflow_reminder.py   (PASSIF : imprime un texte)
   │
   ├─ hook PreToolUse ────────► delegation_gate.py     (BLOQUE : liste étroite)
   │
   ├─ ai_broker.py  ──► router.py ──► providers.py ──► OpenRouter / Gemini / DeepSeek
   │        │                                            (écrit un reçu AVEC provider)
   │        └─► ai_query.py (transport Gemini natif)     (écrit un reçu SANS provider)
   │
   ├─ ai_worker.py  ──► boucle d'outils bornée ──► appelle ai_broker.py en sous-process
   │        └─► worktree git isolé, rend un diff        (écrit un reçu mode=worker)
   │
   └─ explore-leger (agent projet, model: haiku, Read/Glob/Grep)
```

**Deux chemins d'écriture de reçus** coexistent dans le même fichier
`gemini-receipts.jsonl` (nom historique) :

- `ai_broker.py` → reçu **riche** : `provider`, `model`, `candidates`,
  `attempts`, `routing_reason`, `key_id`, `cost_usd`, `quota`…
- `ai_query.py` (appelé **directement**) → reçu **pauvre** : `ts`, `mode`,
  `status`, `tier`, `prompt_chars`, `model`, `fallbacks`… **sans `provider`**.

C'est ce qui explique les 1094 reçus « sans provider » : ce ne sont pas des
reçus cassés, ce sont des appels qui **n'ont pas traversé le broker**.

---

## 3. Points bloquants identifiés (ordonnés par impact)

### 🔴 PB-1 — Le gate ne force jamais l'ouvrier `ai_worker.py`

**Fichier** : [`scripts/hooks/delegation_gate.py`](../../scripts/hooks/delegation_gate.py:188)
(fonction `required_modes`).

`required_modes()` ne retourne **jamais** `("worker",)`. Le mot `worker`
n'apparaît nulle part dans le gate (vérifié : `grep -n "worker"` → 0 résultat).

**Conséquence** : rien n'oblige à confier une tâche « à nombreux appels d'outils
(explorer + modifier + tester) » à l'ouvrier, alors que c'est **le seul exécutant
à 0 jeton Claude** pour ce créneau (cf. skill `workflow` §4 et `CLAUDE.md` pt 61).

**Preuve** : 1 seul reçu `mode=worker` sur 1252. Le commentaire dans
[`ai_worker.py`](../../scripts/ai_worker.py:668) l'assume :
> « ce reçu ne satisfait aucune exigence du gate (qui attend `code`, `test`…) et
> c'est voulu ».

C'est précisément le défaut : **l'ouvrier est hors du champ de contrainte**.

---

### 🔴 PB-2 — Le gate ne force jamais le WBS ni la parallélisation

**Fichier** : [`scripts/hooks/workflow_reminder.py`](../../scripts/hooks/workflow_reminder.py:32).

Le hook `UserPromptSubmit` se contente d'**imprimer** :

```
[Rappel workflow] Requête à plusieurs étapes probable. Charger le skill
'workflow' (cadrage, WBS découpé par LIVRABLE …) avant d'agir.
```

Il ne **bloque rien**, ne **vérifie rien**, ne **trace rien**. Un rappel texte
peut être ignoré sans aucune conséquence. Il n'existe **aucun** mécanisme qui
vérifie qu'un WBS a été produit, ni que des tâches indépendantes ont été lancées
dans le même message.

**Conséquence** : le déroulé en 4 étapes reste une **intention**, pas un
processus. Claude peut répondre à une requête multi-étapes par une suite de
commandes isolées dans le fil principal — exactement le symptôme décrit dans le
skill lui-même (§Pourquoi, diagnostic du 27/09).

---

### 🟠 PB-3 — `Edit` sur un `.py` existant n'est pas gaté

**Fichier** : [`delegation_gate.py`](../../scripts/hooks/delegation_gate.py:211-218).

```python
elif tool_name in ("Write", "Edit"):
    path_obj = Path(tool_input.get("file_path", ""))
    if path_obj.suffix != ".py":
        return None
    if "tests" in path_obj.parts and path_obj.name.startswith("test_"):
        return ("test", "code")
    if tool_name == "Write" and not path_obj.exists():   # ← SEULEMENT Write
        return ("code", "test")
```

Un `Edit` sur un module `.py` **existant** (hors tests) retourne `None` : **non
gaté**. Le commentaire (lignes 194-196) le justifie par « la règle porte sur le
PREMIER JET, pas sur une correction chirurgicale ». Mais en pratique, une
« correction » peut réécrire une fonction entière sans jamais déclencher le
gate. C'est une **porte de sortie majeure** : produire du code dans le fil
principal en éditant l'existant.

---

### 🟠 PB-4 — Le broker est contournable via `ai_query.py` en direct

**Fichiers** : [`ai_query.py`](../../scripts/ai_query.py:643) (transport Gemini),
[`delegation_gate.py`](../../scripts/hooks/delegation_gate.py:156) (`_relaie_au_courtier`).

Le gate considère comme « conforme » toute commande mentionnant
`ai_broker.py` **ou** `ai_query.py` :

```python
_COURTIER = re.compile(r"ai_broker\.py|ai_query\.py")
```

Or `ai_query.py` est le **transport Gemini natif**, qui **ne route pas** vers
OpenRouter. L'appeler en direct satisfait le gate tout en **évitant le
multi-provider**. C'est la cause probable des 1094 reçus sans `provider`.

**Conséquence** : la politique « gratuit d'abord (OpenRouter) » est
**contournable** par un chemin que le gate considère comme légitime.

---

### 🟡 PB-5 — Le gate ne couvre pas le raisonnement ni la production de prose

Aucun déclencheur ne porte sur :

- une réponse de **raisonnement** produite directement par Claude (architecture,
  arbitrage) — pourtant délégable en `--mode reasoning` (payant plafonné) ;
- la rédaction de **prose** (résumé de doc, message de PR) hors `git commit` ;
- l'**exploration** par `Read` chirurgical répété (chaque `Read` ≤ 30 lignes
  passe, mais 20 `Read` successifs reconstituent une lecture large non gatée).

Le gate raisonne **par appel d'outil isolé**, jamais par **accumulation** sur la
requête. Un `Grep` est gaté, mais 15 `Read` chirurgicaux ne le sont pas.

---

### 🟡 PB-6 — `explore-leger` n'est jamais forcé

**Fichier** : [`../.claude/agents/explore-leger.md`](../.claude/agents/explore-leger.md).

L'agent existe (`model: haiku`, `Read`/`Glob`/`Grep`), mais **aucun hook ne
vérifie** qu'un balayage large passe par lui plutôt que par l'agent intégré
`Explore` (qui hérite de `sonnet`) ou par des `Read`/`Grep` directs. Le TODO du
`CLAUDE.md` (pt 62) demande justement de « vérifier dans `/delegation/workflows`
qu'un balayage large sort bien en `haiku` » — ce qui prouve que **ce n'est pas
garanti**.

---

### 🟡 PB-7 — Le suivi de la sous-traitance est aveugle (reçus hétérogènes)

Deux formats de reçus coexistent (cf. §2). Le tableau de bord
[`radar_ops/delegation.py`](../../radar_ops/delegation.py:615) calcule la « part
déléguée » à partir des reçus, mais **1094 reçus n'ont pas de `provider`** : ils
sont donc comptés comme « appels » sans qu'on sache s'ils sont passés par le
broker. **On ne peut pas piloter ce qu'on ne mesure pas** : impossible de savoir
si la sous-traitance OpenRouter progresse ou régresse sans requêter le champ
`provider`.

---

## 4. Ce qui fonctionne déjà (à ne pas casser)

- **Routage gratuit d'abord** : `router.order_candidates()` trie bien
  `free` avant `payant` ([`router.py`](../../scripts/ai/router.py:429)). Vérifié :
  `--list-candidates --mode code` retient les 17 modèles OpenRouter gratuits.
- **Cascade + santé** : `health.py` (cooldown par modèle), `quota.py` (rotation
  de clés, plafond payant `daily_paid_cap_usd: 1.0`).
- **Gate mécanique** : commit, nouveau `.py`, tests, `git diff` brut, `Read`
  large, `Grep` sont bien bloqués sans reçu récent.
- **Ouvrier fonctionnel** : `ai_worker.py` marche (1 reçu `termine`, 6 étapes,
  1 fichier, worktree créé) — il est juste **sous-utilisé**.
- **Reçus riches** du broker : `provider`, `routing_reason`, `cost_usd`,
  `quota` — la donnée existe quand on passe par le broker.

---

## 5. Correctifs proposés (spécification pour l'agent code)

> Principe directeur : **transformer les consignes de prompt en contraintes
> exécutables**, sans casser ce qui marche. Chaque correctif doit être
> **testable** (un cas qui échouait avant, passe après).

### C-1 — Gater l'usage de l'ouvrier sur les tâches multi-outils

**Cible** : [`delegation_gate.py::required_modes`](../../scripts/hooks/delegation_gate.py:188).

Ajouter un déclencheur : si la requête courante (ou la commande) annonce une
tâche « explorer + modifier + tester » (heuristique : présence de `Write`/`Edit`
**suivi** de `Bash` de test dans la même requête, ou compteur d'appels d'outils
de la session dépassant un seuil), exiger un reçu `mode=worker` récent.

**Difficulté** : le gate est `PreToolUse`, il voit un outil à la fois. Deux
options :

- **Option A (simple)** : compter les appels d'outils de la session via
  `telemetry.jsonl` ; au-delà de N appels (ex. 8) sans reçu `worker` récent,
  bloquer le prochain `Write`/`Edit` de `.py` en exigeant `worker`.
- **Option B (robuste)** : introduire un **état de requête** (fichier
  `.claude/current_request.json`) écrit par `workflow_reminder.py` au
  `UserPromptSubmit`, et lu par le gate pour savoir si un WBS a été déclaré.

**Test** : une session qui enchaîne 8 outils sur du `.py` sans `worker` doit
voir son 9ᵉ `Write` bloqué avec un message demandant `ai_worker.py`.

### C-2 — Rendre le WBS vérifiable (pas seulement rappelé)

**Cible** : [`workflow_reminder.py`](../../scripts/hooks/workflow_reminder.py:32).

Faire écrire au hook un **marqueur de requête** (`.claude/current_request.json`)
contenant : horodatage, prompt, `is_multi_step`. Le gate lit ce marqueur :

- si `is_multi_step` et qu'aucun reçu `worker`/`code`/`test` **n'a été écrit
  depuis le début de la requête**, alors bloquer les outils d'écriture.

**Test** : une requête « implémente X et teste-le » sans aucun reçu doit bloquer
le premier `Write`.

### C-3 — Fermer la porte `Edit` sur `.py` existant

**Cible** : [`delegation_gate.py`](../../scripts/hooks/delegation_gate.py:211).

Étendre la condition : un `Edit` sur un `.py` **existant** doit exiger un reçu
récent `code`/`test` **si** l'édition dépasse un seuil (ex. `old_string` +
`new_string` > 40 lignes, ou remplacement d'une fonction entière). En dessous du
seuil (correction chirurgicale), laisser passer — c'est la justification
légitime du commentaire actuel.

**Test** : un `Edit` de 60 lignes sur un module existant sans reçu → bloqué ;
un `Edit` de 5 lignes → passe.

### C-4 — Distinguer `ai_query.py` (direct) de `ai_broker.py` (broker)

**Cible** : [`delegation_gate.py::_relaie_au_courtier`](../../scripts/hooks/delegation_gate.py:156).

Ne plus considérer `ai_query.py` comme un relais conforme **pour les modes
déléguables** (`code`, `test`, `pr`, `context`, `diag`, `search`, `reasoning`).
Réserver `ai_query.py` au seul usage légitime : transport bas niveau appelé
**par** le broker. Le gate doit exiger `ai_broker.py` explicitement.

**Test** : `python3 scripts/ai_query.py --mode code …` sans reçu broker → bloqué
avec message « utiliser ai_broker.py ».

### C-5 — Mesurer la sous-traitance réelle (reçus homogènes)

**Cible** : [`ai_query.py::write_receipt`](../../scripts/ai_query.py:643) et
[`radar_ops/delegation.py`](../../radar_ops/delegation.py:615).

- Ajouter un champ `via` (`"broker"` / `"direct"`) à **tous** les reçus.
- Dans `delegation.py`, calculer la part déléguée **par provider** et exposer
  « % OpenRouter gratuit » vs « % Gemini » vs « % direct ».
- Alerter si le % direct dépasse un seuil (ex. 20 %).

**Test** : un reçu écrit par `ai_query.py` direct porte `via="direct"` ; un reçu
broker porte `via="broker"` + `provider`.

### C-6 — Forcer `explore-leger` sur les balayages larges

**Cible** : nouveau déclencheur dans le gate + TODO pt 62.

Si une requête contient un motif de balayage (« où est », « trouve tous »,
« liste les ») et que le fil principal tente un `Grep` ou un `Read` large, le
gate exige un reçu `mode=search` (courtier) **ou** la trace d'un appel
`explore-leger`. Vérifier dans `/delegation/workflows` que le modèle est bien
`haiku`.

**Test** : un `Grep` sur motif de balayage sans reçu `search` → bloqué.

---

## 6. Plan de vérification (pour l'agent code)

1. **Avant/après** : mesurer sur 5 requêtes réelles, via `/delegation/workflows` :
   - nombre d'appels d'outils du fil principal ;
   - `cache_read_input_tokens` par requête (repère : 2,9 M, 137 k/appel) ;
   - nombre de reçus `worker` et `provider=openrouter`.
2. **Tests unitaires** : chaque correctif C-1..C-6 doit avoir un test qui
   **échouait avant** (le gate laissait passer) et **passe après** (le gate
   bloque). Suivre la convention du dépôt : `tests/test_*.py`, `unittest`.
3. **Non-régression** : la suite entière doit rester verte
   (`.venv/bin/python -m unittest discover -s tests`).
4. **Garde-fou** : ne pas gater les corrections chirurgicales légitimes (seuils
   explicites), sinon Claude sera bloqué sur des micro-éditions et la règle sera
   contournée autrement.

---

## 7. Fichiers à modifier (récapitulatif)

| Fichier | Correctif | Nature |
|---|---|---|
| [`scripts/hooks/delegation_gate.py`](../../scripts/hooks/delegation_gate.py:188) | C-1, C-3, C-4, C-6 | cœur du gate |
| [`scripts/hooks/workflow_reminder.py`](../../scripts/hooks/workflow_reminder.py:32) | C-2 | marqueur de requête |
| [`scripts/ai_query.py`](../../scripts/ai_query.py:643) | C-5, C-7 | champ `via` + CLI interne |
| [`radar_ops/delegation.py`](../../radar_ops/delegation.py:615) | C-5 | mesure par provider |
| [`scripts/ai_worker.py`](../../scripts/ai_worker.py:665) | C-1 | reçu `worker` exploitable par le gate |
| [`scripts/hooks/gemini_gate.py`](../../scripts/hooks/gemini_gate.py:1) | C-7 | **supprimer** (alias mort) |
| [`scripts/gemini_gateway.py`](../../scripts/gemini_gateway.py:1) + [`cloud-gemini-gateway.sh`](../../scripts/cloud-gemini-gateway.sh:1) | C-7 | **supprimer** (inertes sur VPS) |
| [`scripts/bench_ai_query.py`](../../scripts/bench_ai_query.py:1) | C-7 | **supprimer** (doublon du banc broker) |
| [`.claude/skills/ask-gemini/`](../.claude/skills/ask-gemini/SKILL.md:1) | C-7 | **supprimer** (alias de `delegate`) |
| [`.claude/settings.json`](../.claude/settings.json:1) | C-7 | retirer l'appel `cloud-gemini-gateway.sh` |
| [`scripts/loop/registry.json`](../../scripts/loop/registry.json:1) | C-7 | retirer l'entrée `ai_query` |
| [`CLAUDE.md`](../../CLAUDE.md:21) | doc | aligner la description du gate |
| [`tests/test_*.py`](../../tests/) | tous | cas avant/après |

---

## 8. Nettoyage & consolidation de l'écosystème (demande utilisateur)

> Demande ajoutée : « trop de scripts différents, certains ne sont même plus
> utilisés, d'autres font appel à Gemini alors que Gemini n'est plus le seul
> modèle. Mettre tout ça au propre et améliorer les résultats de délégation. »

### 8.1 Inventaire — ce qui est vivant, ce qui est mort

| Fichier | Lignes | État | Preuve |
|---|---|---|---|
| [`ai_broker.py`](../../scripts/ai_broker.py:1) | 1628 | **VIVANT** — porte d'entrée | appelé par le gate, les skills, `ai_worker` |
| [`ai_query.py`](../../scripts/ai_query.py:1) | 838 | **VIVANT mais mal utilisé** — transport Gemini bas niveau | importé par `providers.py`, `envelope.py`, `prompts.py`, `quota.py` |
| [`ai_worker.py`](../../scripts/ai_worker.py:1) | 935 | **VIVANT mais sous-utilisé** | 1 reçu `worker` / 1252 |
| [`ai/`](../../scripts/ai/) (16 modules) | ~2000 | **VIVANT** — cœur du broker | router, catalogue, providers, quota, health… |
| [`hooks/delegation_gate.py`](../../scripts/hooks/delegation_gate.py:1) | 528 | **VIVANT** — gate actif | branché dans `settings.json` |
| [`hooks/workflow_reminder.py`](../../scripts/hooks/workflow_reminder.py:1) | 68 | **VIVANT mais passif** | branché dans `settings.json` |
| [`hooks/gemini_gate.py`](../../scripts/hooks/gemini_gate.py:1) | 50 | **MORT (alias)** | simple réexport de `delegation_gate` ; absent de `settings.json` |
| [`gemini_gateway.py`](../../scripts/gemini_gateway.py:1) | 219 | **MORT sur VPS** | `CLAUDE_CODE_REMOTE≠true` → jamais joué |
| [`cloud-gemini-gateway.sh`](../../scripts/cloud-gemini-gateway.sh:1) | 66 | **MORT sur VPS** | `exit 0` immédiat hors cloud |
| [`bench_ai_query.py`](../../scripts/bench_ai_query.py:1) | 478 | **OBSOLÈTE** | bench de l'ancien transport seul, doublonné par `bench_ai_broker.py` |
| [`.claude/skills/ask-gemini/SKILL.md`](../.claude/skills/ask-gemini/SKILL.md:1) | 31 | **MORT (alias)** | alias de `delegate` |
| [`bench_ai_broker.py`](../../scripts/bench_ai_broker.py:1) | 3018 | VIVANT | banc du broker |
| [`bench_ai_worker.py`](../../scripts/bench_ai_worker.py:1) | 907 | VIVANT | banc de l'ouvrier |

### 8.2 Le problème « Gemini partout » (résidu historique)

Le nom `gemini-*` est **partout** alors que Gemini n'est plus qu'**un** provider
parmi OpenRouter/DeepSeek/xAI :

- fichier de reçus : `gemini-receipts.jsonl` (nom historique, cf. commentaire
  dans [`delegation_gate.py`](../../scripts/hooks/delegation_gate.py:10)) ;
- hook : `gemini_gate.py` (alias) ;
- gateway : `gemini_gateway.py` + `cloud-gemini-gateway.sh` ;
- skill : `ask-gemini` ;
- bench : `bench_ai_query.py` ;
- variable d'env : `RADAR_GEMINI_GATEWAY_PORT`.

**Ce n'est pas qu'esthétique** : un lecteur (humain ou agent) croit que Gemini
est le seul modèle, et les nouveaux venus recopient le mauvais point d'entrée
(`ai_query.py` au lieu de `ai_broker.py`) — ce qui alimente PB-4 (87 % d'appels
directs).

### 8.3 Plan de nettoyage proposé (C-7)

**Étape 1 — Supprimer les morts confirmés** (aucun appelant vivant) :

- `scripts/hooks/gemini_gate.py` → supprimer, après avoir migré
  `tests/test_gemini_gate.py` pour importer `delegation_gate` directement.
- `scripts/gemini_gateway.py` + `scripts/cloud-gemini-gateway.sh` → supprimer,
  après avoir retiré l'appel dans `settings.json` (SessionStart) et les
  références dans `tests/test_ai_query.py`.
- `.claude/skills/ask-gemini/` → supprimer (alias de `delegate`).
- `scripts/bench_ai_query.py` → supprimer, après avoir retiré l'entrée
  `ai_query` de [`scripts/loop/registry.json`](../../scripts/loop/registry.json:1)
  (le banc du broker couvre déjà le transport).

**Étape 2 — Renommer le vocabulaire « gemini » en « ai »** (optionnel, plus
lourd) :

- `gemini-receipts.jsonl` → `ai-receipts.jsonl` : **dangereux**, car le nom est
  lu par le gate, `radar_ops` et les reçus historiques. À faire **en une seule
  PR** avec migration (lecture des deux noms pendant une transition), ou **ne
  pas faire** (garder le nom historique documenté). Recommandation : **garder**
  le nom, mais corriger tous les commentaires qui laissent croire que c'est
  Gemini-only.

**Étape 3 — Clarifier `ai_query.py`** :

- Le garder comme **transport bas niveau** (il est bien encapsulé par
  `providers.py`).
- **Retirer son CLI public** ou le rendre explicitement « interne » : le gate
  ne doit plus le considérer comme un relais conforme (cf. C-4). Idéalement,
  `ai_query.py` n'est plus appelé que par import, jamais en ligne de commande.

**Étape 4 — Un seul point d'entrée documenté** :

- `CLAUDE.md`, skills et `registry.json` ne doivent citer que
  `ai_broker.py` (délégation) et `ai_worker.py` (exécution autonome).
- `ai_query.py` n'apparaît que dans la section « interne » du skill `delegate`.

### 8.4 Tests à adapter lors du nettoyage

| Test | Action |
|---|---|
| [`tests/test_gemini_gate.py`](../../tests/test_gemini_gate.py:1) | réimporter `delegation_gate` (plus `gemini_gate`) |
| [`tests/test_ai_query.py`](../../tests/test_ai_query.py:1) | retirer les cas `gemini_gateway` |
| [`scripts/loop/registry.json`](../../scripts/loop/registry.json:1) | retirer l'entrée `ai_query` (ou la fusionner dans `ai_broker`) |

### 8.5 Lien avec l'amélioration de la délégation

Le nettoyage **sert directement** l'objectif « améliorer les résultats de
délégation » :

- **Moins de chemins = moins de contournements** : supprimer `ai_query.py` du
  CLI et `gemini_gate.py` réduit les portes de sortie de la RÈGLE N°1 (PB-4).
- **Un seul point d'entrée** (`ai_broker.py`) rend le gate **plus simple à
  durcir** (C-1..C-6) : il n'a plus à gérer deux noms de relais.
- **Vocabulaire neutre** : un agent qui lit `delegation_gate` / `ai-receipts`
  comprend qu'il y a plusieurs providers et route correctement.

---

## 9. Conclusion

Le dispositif n'est **pas cassé** : le courtier route correctement, l'ouvrier
fonctionne, les reçus existent. Le défaut est **structurel** : la règle
« déléguer d'abord » est **une consigne de prompt**, appliquée par la seule
vigilance du modèle, alors que les seules contraintes **exécutables** (le gate)
portent sur des déclencheurs mécaniques étroits qui **laissent hors champ** :

- l'ouvrier `ai_worker.py` (1 usage / 1252) ;
- le WBS et la parallélisation ;
- l'édition de code existant ;
- le contournement du broker par `ai_query.py` direct (87 % de l'historique) ;
- `explore-leger`.

**Le correctif central est donc d'étendre le gate** pour qu'il contraigne le
**pattern** (WBS → ouvrier/parallèle → assemblage), pas seulement des **actes**
isolés. Tant que ce n'est pas fait, le taux de sous-traitance restera dépendant
de la bonne volonté du modèle.
