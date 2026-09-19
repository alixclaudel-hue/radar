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
  scripts/loop/registry.json, pas seulement la recherche YouTube. Session cloud
  uniquement : c'est elle qui écrit le code (contrat vps-ops).
---

# Boucle d'amélioration d'un script

Six étapes, dans l'ordre. Chacune a une condition d'arrêt : la boucle préfère
s'arrêter et le dire plutôt que d'avancer sans mesure.

**Le principe qui tient tout** : aucun correctif n'est gardé sans un cas de banc
qui échouait AVANT lui et passe APRÈS. Sans ça on ne corrige rien, on déplace du
code. C'est la leçon du 19/09 — un corpus de 25 cas construit depuis des succès
de production passait encore avec le scoring entièrement court-circuité.

La boucle traverse deux environnements que le contrat `vps-ops` sépare : le VPS
observe (il a les données réelles, jamais le droit d'écrire du code), le cloud
corrige (il écrit le code, n'a aucun accès au VPS). Le dépôt **`radar-diag`** est
le bus entre les deux : le VPS y pousse ses lots, le cloud les y lit.

---

## Étape 0 — Quel script

L'argument arrive après `ARGUMENTS:`. Sans argument, liste les scripts de
`scripts/loop/registry.json` et demande lequel.

Lis l'entrée du registre et retiens :

```
module     = <module principal>
bench      = python3 scripts/loop/bench.py <script>
touchable  = <liste des fichiers modifiables>
obs_dir    = radar-diag/observations/<script>/
diag       = docs/diagnostics/<script>-<AAAA-MM-DD>.md
branche    = claude/loop-<script>-<AAAA-MM-DD>
```

Un script absent du registre n'entre pas dans la boucle : il lui faut d'abord un
banc et une source d'observations. Dis-le, propose de les créer, et arrête-toi.

---

## Étape 1 — Récupérer les observations

`radar-diag` est un dépôt distinct. S'il n'est pas dans la session, attache-le
(`add_repo` puis clone), sinon `git pull`.

Prends le lot le plus récent de `obs_dir`, lis son `meta.json`.

**Branche A — aucun lot.** La boucle n'a rien à diagnostiquer. Écris la demande
de collecte dans `docs/diagnostics/<script>-collecte-demandee.md` (une phrase :
quel script, quelles familles d'échecs, plafond de quota), dis à l'utilisateur
de lancer `/script-observe <script>` sur sa session VPS, et **arrête-toi**.

**Branche B — lot plus vieux que le dernier déploiement.** Compare
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

Pour chaque finding retenue, dans l'ordre du document :

1. Écris le cas dans les fixtures du script, avec `"kind": "adversarial"` et les
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

Ouvre la PR avec, dans la description : le lot d'observations utilisé, les
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

**La boucle s'arrête à la PR.** Le merge déclenche le déploiement en production
(`deploy.yml`) : c'est le dernier cran où un humain peut dire non, et il lui
revient. Dis à l'utilisateur quelles portes passent, lesquelles non, et laisse-le
trancher.

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
