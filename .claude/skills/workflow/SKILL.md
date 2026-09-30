---
name: workflow
description: >
  Déroulé systématique de TOUTE requête utilisateur sur ce dépôt (décision
  utilisateur du 27/09/2026) : 1. Analyse & cadrage, 2. WBS, 3. Ordonnancement
  (séquence vs parallèle), 4. Délégation de chaque tâche à l'exécutant le plus
  adapté (mode du courtier `scripts/ai_broker.py`, ou ouvrier `scripts/ai_worker.py`
  — qui passe lui-même par le courtier — ou agent projet `explore-leger` en
  lecture seule), puis consolidation par Claude. S'applique sans attendre de
  demande explicite ; son
  but chiffré est de baisser le cache relu par le fil principal.
---

# Workflow par requête — cadrer, découper, ordonnancer, déléguer

## Pourquoi

Mesure du 27/09 (cartographie `/delegation/workflows`, 20 requêtes, 426 appels
API) : **58,3 M jetons de cache relu contre 1,3 M de travail frais**. Le fil
principal démarre à ~62k jetons de contexte et monte à 200-230k en session
longue ; chaque appel d'outil relit toute cette fenêtre. Un ouvrier frais
démarre à ~0 jeton Claude et ne renvoie au fil principal qu'un rapport court et
un diff ; un appel au courtier ne coûte aucun jeton Claude. Le levier n'est donc
pas de mieux écrire, c'est de **faire moins d'allers-retours d'outils dans le
fil principal**.

Diagnostic du 27/09 (voir `.claude/gemini-receipts.jsonl` et un cas concret :
régénération de `docs/app-overview.md`) : le fil principal enchaînait 3 `Bash`
de vérif d'état (hash git, méta, existence du fichier) + 1 `Agent` +
2 `Read` intégraux du fichier généré — 6 tours pour UNE tâche conceptuelle.
Le découpage se faisait par COMMANDE SHELL, pas par LIVRABLE : voir §2.

Ce rappel n'est plus laissé à la seule mémoire de Claude : le hook
`UserPromptSubmit` (`scripts/hooks/workflow_reminder.py`) l'injecte
automatiquement sur toute requête qui contient un verbe d'action ou dépasse
60 caractères sans être une simple question — une requête chatbot pure
("c'est quoi X ?") n'est pas rappelée.

## Les 4 étapes

### 1. Analyse & cadrage (fil principal, court)

Besoin reformulé en une phrase, critère de fin vérifiable, périmètre exclu.
Si une décision revient réellement à l'utilisateur, la poser MAINTENANT
(une seule question groupée), pas au milieu de l'exécution.

### 2. WBS

Découper en tâches à périmètre fermé : chacune a une entrée (fichiers, données),
une sortie (fichier écrit, verdict, chiffre) et un critère de fin. Une tâche qui
ne tient pas en un prompt autonome de ~15 lignes est à redécouper.

**Le WBS chiffre aussi le nombre de tours prévus au fil principal et cherche à
le minimiser** — chaque appel d'outil du fil principal relit tout son contexte
(cache qui grandit, cf. §Pourquoi). Fusionner deux tâches strictement
séquentielles sans jugement intermédiaire en un seul prompt délégué plutôt que
deux allers-retours. Ne dupliquer un exécutant en comparaison (§4) que pour une
tâche identifiée comme complexe : chaque exemplaire supplémentaire est un coût
à justifier, jamais un réflexe.

**Le découpage se fait par LIVRABLE, jamais par commande shell.** Une
vérification d'état qui sert seulement à DÉCIDER si le livrable doit être
produit (hash git, existence d'un fichier, taille d'un diff, méta de
fraîcheur) fait partie du prompt de la tâche déléguée qui produit ce livrable
— elle ne devient pas un tour séparé du fil principal avant délégation.
Exemple : « régénérer `docs/app-overview.md` si le dépôt a changé depuis sa
dernière génération » est UNE tâche pour un exécutant (il vérifie lui-même
le hash/la méta dans son contexte frais et décide), jamais 3 `Bash` de
vérification + 1 `Agent`.

### 3. Ordonnancement

Tableau court : tâche → dépend de → exécutant. Les tâches sans dépendance entre
elles partent **dans le même message** (plusieurs ouvriers en arrière-plan
et/ou appels courtier `run_in_background`), les autres attendent leur prérequis.

### 4. Délégation, puis consolidation

| Nature de la tâche | Exécutant | Pourquoi |
|---|---|---|
| Un seul jet de texte : premier jet de code/tests, résumé de doc, diagnostic de log, message de PR | courtier, mode `code`/`test`/`context`/`diag`/`pr` | 0 jeton Claude |
| Question fermée sur le dépôt (où est X ?) | courtier `--mode search` | 0 jeton Claude |
| Raisonnement que le gratuit rate | courtier `--mode reasoning` | payant, plafonné |
| Balayage large de fichiers, conclusion seule utile | agent projet `explore-leger` (`model: haiku`) | lecture seule, contexte frais, modèle léger — l'agent intégré `Explore` hérite du modèle de session (sonnet) et n'est pas modifiable |
| Tâche à nombreux appels d'outils (explorer + modifier + tester) | **ouvrier `scripts/ai_worker.py`**, un par tâche du WBS | **0 jeton Claude** : boucle bornée pilotée par un modèle gratuit via le courtier, worktree isolé, rend un diff à relire |
| Chantier de plusieurs livrables (code, tests, doc) où le choix du modèle compte | agent **`delegation-orchestrateur`** (`model: opus`) | découpe, attribue chaque tâche au modèle mesuré le meilleur pour sa nature (grille du banc du 30/09 : `mode_default_models`/`mode_cascades` de `config/ai_models.json`), règle `--effort` selon la difficulté, vérifie et escalade ; ne produit rien lui-même, tout sort du courtier ou d'un ouvrier |
| Jugement : architecture, sécurité, arbitrage produit, relecture finale, commit, merge | fil principal | non délégable (RÈGLE N°1) |

**L'exécutant est l'ouvrier `ai_worker.py`, pas un sous-agent Claude.** Un
sous-agent Claude Code ne peut être configuré que sur un modèle Anthropic (le
champ `model:` n'accepte que `sonnet`/`opus`/`haiku`/`inherit`) : le garder en
Sonnet « pour orienter » revient à piloter l'exécution par Claude — ce que la
décision utilisateur du 27/09 a écarté. `ai_worker.py` appelle lui-même le
courtier (`_decider_courtier`), écrit dans un `git worktree` dédié et rend un
diff ; le fil principal ne paie aucun jeton Claude pour la production.

**Le balayage en lecture seule, c'est `explore-leger`, pas `Explore`.** L'agent
intégré `Explore` de Claude Code est codé en dur avec `model: inherit` : il
hérite du modèle de la session (sonnet) et rien dans `.claude/` ne peut
l'abaisser. Le créneau « balayage large, conclusion seule utile » est donc tenu
par l'agent projet `.claude/agents/explore-leger.md` (`model: haiku`, outils
`Read`/`Glob`/`Grep`) — fichier versionné, aucun effet de bord sur les autres
sous-agents. S'il faut *écrire* ou *tester*, c'est un ouvrier (`ai_worker.py`,
0 jeton Claude), pas lui ; pour un simple « où est X ? », le courtier
`--mode search` suffit (0 jeton Claude).

Lancer un ouvrier :

```bash
python3 scripts/ai_worker.py "<tâche autonome>" --json
# comparaison : un worktree/branche DISTINCTS par exemplaire
python3 scripts/ai_worker.py "<tâche>" --worktree .worktrees/cmp-a --branche cmp/a --json
```

Il rend un rapport JSON (`statut`, `fichiers_modifies`, `commandes`, `diff`,
`escalade_disponible`, `paye`) et, avec `--diff`, le diff unifié. Bornes dures :
étapes, durée, jetons estimés, fichiers — les atteindre est un arrêt avec
rapport, jamais un échec silencieux. Escalade payante (DeepSeek) uniquement sur
`--escalate-paid`, jamais automatique.

Règles de délégation :

- **Jamais `subagent_type: "fork"`** : il hérite du contexte complet (mesuré à 180k au démarrage) et annule le gain.
- Le prompt d'une tâche déléguée est **autonome** : répertoire de travail, fichiers et lignes concernés, ce qui est déjà établi, critère de fin, format de retour. Il n'a pas la conversation.
- L'ouvrier applique lui-même la RÈGLE N°1 (le courtier avant lui) — c'est son mode de décision par défaut, le rappeler n'est pas nécessaire.
- Pas d'ouvrier pour une tâche de 1 à 3 appels d'outils : son démarrage coûte plus que la tâche faite sur place.
- **Plusieurs ouvriers en parallèle, un par tâche indépendante du WBS** (cf. §3) : chaque tâche produit son propre worktree (`worker-<slug>`) ; c'est Claude (fil principal) qui assemble ensuite les rapports et les diffs.
- **Comparaison ciblée** : pour une tâche complexe (spec ambiguë, sécurité, cœur d'algorithme, aucun test de référence pour trancher), lancer 2 à 3 ouvriers sur EXACTEMENT la même tâche, **avec des `--worktree`/`--branche` distincts** (sinon ils se marchent dessus). Le fil principal compare les diffs et choisit ou fusionne la meilleure solution — jamais par défaut, seulement quand la tâche le justifie.
- L'ouvrier ne `commit` ni ne `push` : la consolidation, le commit et le merge restent au fil principal (jugement non délégable).

Consolidation (fil principal) : relire les rapports, ouvrir de façon
chirurgicale (`limit` ≤ 30) uniquement le douteux, appliquer ou adapter le diff
retenu, trancher, lancer la vérification finale (suite de tests), commit/PR via
le mode `pr`.

## Hygiène de session (même objectif)

- Requête sans lien avec la précédente → proposer `/clear` avant de commencer : une session qui enchaîne les sujets relit à chaque appel l'historique de tous les sujets précédents.
- Ne jamais relire un fichier déjà lu ou éditer « pour vérifier » ; ne pas rapatrier de sortie d'outil volumineuse dans le fil principal (la faire résumer par le courtier ou la tronquer à la source).

## Trace attendue dans la réponse

Pour une requête de plus d'une tâche, la réponse finale montre en quelques
lignes le WBS avec, par tâche, l'exécutant réel (mode courtier, ouvrier, ou
fil principal + pourquoi) et le nombre d'exemplaires lancés (1, ou 2-3 en
comparaison + pourquoi celle-ci le justifiait). C'est ce qui permet de vérifier
après coup, dans `/delegation/workflows`, que le cache relu baisse vraiment et
que les comparaisons restent l'exception.
