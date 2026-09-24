---
name: script-loop
description: >
  Boucle d'amélioration d'un script de Radar à partir de son comportement réel :
  récupère les observations de production collectées sur le VPS, diagnostique les
  échecs via l'agent script-doctor, transforme chaque défaut en cas de banc qui
  échoue AVANT correctif, corrige, remesure, et ne garde que ce qui améliore
  réellement. Utiliser quand on demande d'améliorer, corriger, optimiser ou faire
  progresser un script d'après ses erreurs en production — par exemple
  « /script-loop ytcache ». Fonctionne pour tout script inscrit à
  scripts/loop/registry.json, pas seulement la recherche YouTube. Utilisable
  par la session cloud comme par la session VPS — cette dernière travaille dans
  un clone séparé, ~/radar-work, jamais dans le checkout de production
  (contrat vps-ops).
---

# Boucle d'amélioration d'un script

Six étapes, dans l'ordre. Chacune a une condition d'arrêt : la boucle préfère
s'arrêter et le dire plutôt que d'avancer sans mesure.

**Le principe qui tient tout** : aucun correctif n'est gardé sans un cas de banc
qui échouait AVANT lui et passe APRÈS. Sans ça on ne corrige rien, on déplace du
code. C'est la leçon du 19/09 — un corpus de 25 cas construit depuis des succès
de production passait encore avec le scoring entièrement court-circuité.

**Qui peut la lancer, et où.** Les deux sessions, sous la même règle : on ne
corrige jamais le code dans un checkout servi en production.

| Session | Dépôt de travail | Observations |
|---|---|---|
| cloud (session archivée) | son clone habituel | lues sur `radar-diag` (l'attacher via `add_repo`) |
| VPS | **`~/radar-work`**, jamais `~/radar` | déjà sur place, ou collectées par `/script-observe` |

Côté VPS, le périmètre exact est celui de la section « Boucle d'amélioration
d'un script » du contrat `vps-ops` — lis-la avant de commencer.

**Une seule boucle à la fois par script.** Avant de démarrer, vérifie qu'aucune
PR de boucle n'est déjà ouverte sur ce script : deux sessions qui corrigent les
mêmes fichiers en parallèle, c'est le conflit que la séparation historique
existait pour empêcher.

Lis `.claude/skills/ask-gemini/SKILL.md` d'abord si tu ne l'as pas déjà en mémoire.
---

## Étape 0 — Quel script

L'argument arrive après `ARGUMENTS:`. Sans argument, liste les scripts de
`scripts/loop/registry.json` et demande lequel.

Utilise /ask-gemini pour lire l'entrée du registre et retiens :

```
module     = <module principal>
bench      = python3 scripts/loop/bench.py <script>
touchable  = <liste des fichiers modifiables>
obs_dir    = <radar-diag>/observations/<script>/
diag       = docs/diagnostics/<script>-<AAAA-MM-DD>.md
branche    = loop/<script>-<AAAA-MM-DD>
```

Et le dépôt de travail, selon qui tourne : le clone habituel côté cloud,
`~/radar-work` côté VPS (jamais `~/radar`).

Un script absent du registre n'entre pas dans la boucle : il lui faut d'abord un
banc et une source d'observations. Dis-le, propose de les créer, et arrête-toi.

---

## Étape 1 — Récupérer les observations

`radar-diag` est un dépôt distinct du dépôt applicatif. Côté cloud, attache-le
(`add_repo` puis clone) s'il n'est pas dans la session, sinon `git pull`. Côté
VPS, il est déjà là (`~/radar-diag`) : `git pull`.

Prends le lot le plus récent de `obs_dir`, utilise ask-gemini mode read pour lire son `meta.json`.

**Branche A — aucun lot.** La boucle n'a rien à diagnostiquer.
- Côté VPS : lance la collecte toi-même (`/script-observe <script>`), puis
  reprends ici.
- Côté cloud (session archivée): écris la demande dans
  `docs/diagnostics/<script>-collecte-demandee.md` (une phrase : quel script,
  quelles familles d'échecs, plafond de quota), dis à l'utilisateur de lancer
  `/script-observe <script>` sur sa session VPS, et **arrête-toi**.

**Branche B — lot plus vieux que le dernier déploiement.** Utilise ask-gemini pour comparer
`meta.deployed_sha` au `HEAD` de `main`. Si du code du périmètre `touchable` a
bougé depuis, le lot décrit un comportement qui n'existe plus : signale-le et
demande une collecte fraîche avant d'aller plus loin.

**Branche C — lot exploitable.** Continue.

Un lot dont tous les `expected_hint` valent `null` permet de compter, pas de
tester : dis-le, et attends-toi à ne produire aucun cas de banc à l'étape 3.

---

## Étape 2 — Diagnostiquer

Lance l'agent `script-doctor` (outil Agent, `subagent_type: "script-doctor"`).
Il travaille dans son propre contexte et **écrit un fichier** — il ne rapporte
rien. Donne-lui, en toutes lettres (il ne connaît pas cette conversation) : le
nom du script, le chemin absolu du lot, le chemin `<diag>` à écrire, et le
rappel qu'il ne modifie aucun code.

Attends la fin, puis lis `<diag>`.

Si le document ne contient aucune finding, la boucle s'arrête là : c'est un bon
résultat, dis-le en une ligne.

---

## Étape 3 — Transformer chaque finding en cas de banc

Pour chaque finding retenue, demande une lecture à /ask-gemini en mode read et summary dans l'ordre du document :

1. Écris le cas en utilise /ask-gemini mode code dans les fixtures du script, avec `"kind": "adversarial"` et les
   charges utiles du lot (`payloads`) comme fixtures de rejeu.
2. **Lance le banc AVANT tout correctif.** Le nouveau cas DOIT échouer.
   - Il échoue → le cas prouve quelque chose, garde-le.
   - Il passe déjà → le correctif ne sert à rien, ou le cas ne teste pas ce
     qu'il prétend. Ne code pas le correctif : note-le dans `<diag>` et passe
     au suivant. **Ne contourne jamais cette règle** ; c'est la seule protection
     contre un cas écrit d'après le comportement qu'on s'apprête à produire.

Enregistre la mesure de départ :
```
python3 scripts/loop/bench.py <script> --save /tmp/loop-<script>-avant.json
```

---

## Étape 4 — Corriger et remesurer

Une finding à la fois, jamais un lot de correctifs mélangés — sinon on ne sait
plus lequel a produit quoi.

Pour chaque finding : applique le correctif proposé (dans le périmètre
`touchable`), puis

```
python3 scripts/loop/bench.py <script> --compare /tmp/loop-<script>-avant.json
```

- **AMÉLIORATION** → garde, commit, passe à la finding suivante.
- **NEUTRE** → le correctif ne fait rien de mesurable. Annule-le. Soit le cas
  manque, soit la cause racine était mal vue : note-le dans `<diag>`.
- **RÉGRESSION** → annule. Si la finding reste crédible, cherche un correctif
  plus étroit ; sinon note-la comme non traitée.

Entre chaque itération, le code revient à un état mesuré — jamais un empilement
de tentatives non évaluées.

---

## Étape 5 — Les portes, puis la PR

Ouvre la PR en utilisant /ask-gemini mode pr, la description doit intégrer : le lot d'observations utilisé, les
findings traitées et écartées, et les mesures avant/après **verbatim**.

Vérifie les **cinq portes**, une par une, explicitement, et **écris leur état
dans la PR** — c'est ce qui permet à l'utilisateur de décider en un coup d'œil :

| Porte | Vérification |
|---|---|
| G1 — gain mesuré | verdict `--compare` = AMÉLIORATION (ni NEUTRE, ni RÉGRESSION) |
| G2 — cas prouvés | chaque cas ajouté échouait avant le correctif (étape 3) |
| G3 — rayon d'explosion | `git diff --name-only` ⊆ `touchable` du registre |
| G4 — non-régression projet | `python3 -m unittest discover -s tests` sans échec NOUVEAU |
| G5 — CI verte | les checks requis passent sur la PR |

**Le merge est un geste que tu peux exécuter, jamais une décision que tu prends.**
Présente l'état des cinq portes à l'utilisateur et attends son instruction
explicite pour CETTE PR — jamais une règle générale (« si tout est vert,
merge »), jamais une validation implicite reprise d'un accord donné plus tôt
pour autre chose dans la conversation. Redemande en cas de doute. Une fois
l'instruction reçue sans ambiguïté, exécute le merge (`gh pr merge`) : ça
déclenche le déploiement en production (`deploy.yml`), c'est pour ça que la
décision reste systématiquement humaine même si le geste ne l'est plus.

Ne force jamais une porte, ne l'élargis jamais pour faire passer un correctif :
une porte contournée vaut une porte absente.

**Ce que ces portes ne couvrent pas** — à dire, pas à taire : le banc ne teste
qu'un chemin du script sur les cas collectés ; un correctif peut améliorer la
mesure et dégrader un comportement que personne n'a encore observé. Le filet en
aval reste le health-check du déploiement et son rollback — pas le banc.

---

## Étape 6 — Refermer la boucle

Une correction mesurée hors-ligne n'est pas une correction vérifiée. Une fois le
déploiement passé, écris la demande de ré-observation : `/script-observe
<script>`, pour confirmer que le taux d'échec baisse **en production** — c'est la
seule mesure qui compte vraiment.

Puis mets à jour `CLAUDE.md` : ce qui a été corrigé, ce qui a été écarté et
pourquoi, ce qu'il reste à vérifier sur le VPS.

---

## Conditions d'arrêt (récapitulatif)

La boucle s'arrête proprement, sans rien merger, dans tous ces cas — et c'est
normal :

- script absent du registre (pas de banc, pas de mesure possible) ;
- aucun lot d'observations, ou lot périmé par un déploiement ;
- aucune finding dans le diagnostic ;
- un cas de banc qui passe déjà avant correctif ;
- verdict NEUTRE ou RÉGRESSION ;
- une porte de l'étape 5 qui ne passe pas.
