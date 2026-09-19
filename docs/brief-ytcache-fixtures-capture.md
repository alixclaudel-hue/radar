# (remplacé) Brief ponctuel de capture ytcache

Ce brief a servi une fois, le 19/09 — 40 cas capturés depuis
`recos_playlist_history.json`, dont 25 intégrés au dépôt. Il est **remplacé par
un process générique**, valable pour tous les scripts et pas seulement la
recherche YouTube :

| Rôle | Où |
|---|---|
| Collecter les observations de production | `.claude/skills/script-observe/SKILL.md` (session VPS) |
| Diagnostiquer, corriger, remesurer | `.claude/skills/script-loop/SKILL.md` (session cloud) |
| Analyser un lot d'échecs | `.claude/agents/script-doctor.md` |
| Scripts éligibles + périmètres modifiables | `scripts/loop/registry.json` |
| Mesurer / comparer avant-après | `scripts/loop/bench.py` |

**Ce que ce brief a appris, et qui est la raison d'être du nouveau process** :
il demandait de capturer des **succès** de production pour en faire un corpus de
non-régression. Mesuré ensuite : ces 25 cas passent encore avec le scoring de
`_best_match` entièrement court-circuité — YouTube renvoyant déjà la bonne vidéo
en première position pour une requête « Artiste Titre » bien formée. Ils ne
mesurent donc pas notre code, seulement la recherche YouTube. Ils restent comme
plancher (détection d'une casse franche), jamais comme mesure de progrès.

Le nouveau process collecte des **échecs**, pas des succès (cf. l'en-tête de
`script-observe`), et refuse tout correctif qui ne fait pas basculer un cas de
banc qui échouait avant lui.
