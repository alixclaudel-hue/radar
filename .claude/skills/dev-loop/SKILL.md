---
name: dev-loop
description: Boucle dev cloud ↔ diagnostic VPS. À lire par la session de dev cloud après un merge sur main, pour décider s'il faut déclencher un diagnostic sur le VPS, comment le déclencher, et comment consommer le rapport. L'utilisateur ne parle qu'à la session cloud.
---

# Boucle dev cloud ↔ diag VPS

Tu es la session de dev cloud. Tu codes, tu ouvres les PR, tu merges. Tu ne vois
ni la base réelle, ni les conteneurs, ni les jobs : c'est la session de
diagnostic sur le VPS qui les voit (contrat : `.claude/skills/diag/SKILL.md`).

**L'utilisateur ne parle qu'à toi.** C'est toi qui réveilles la session diag,
qui lis son rapport, qui corriges, et qui lui rends une synthèse. Il ne relaie
rien à la main.

## Quand déclencher un diagnostic

Après un merge sur `main`, c'est **ton jugement**, pas un réflexe. Le critère :
*est-ce que ce changement peut casser d'une façon qui ne se voit qu'avec les
données réelles ?*

**Toujours** — schéma ou pipeline d'import, jobs de fond, tout ce qui lit
`/data`, scoring dont le résultat dépend du volume réel, migrations, config de
déploiement, dépendance à une API externe.

**Jamais** (gaspillage de quota) — libellés, CSS, restructuration de template
sans changement de route, docs, tests seuls.

**Au jugé** — nouvelles routes, refonte d'un helper partagé, performance.

**Règle qui prime sur les autres** : si le diagnostic précédent a remonté un
bloquant, le merge suivant est diagnostiqué d'office — il faut vérifier que le
correctif a bien atterri en réel.

## Comment déclencher

1. Ouvre une issue GitHub `Diag <sha court> — <résumé du changement>`, avec le
   label `diag`. Une issue par déploiement : l'historique reste lisible.
2. Abonne-toi à son activité (le commentaire du rapport te réveillera).
3. Déclenche la routine `diag-vps` (`fire_trigger`) en passant en `text` :

```
sha=<sha complet> · issue=<numéro>
scope: <les chemins/comportements touchés, en une ou deux phrases>
attention: <ce qui te paraît le plus à risque, ou "rien de particulier">
```

Le `scope` est ce qui fait la différence entre un diagnostic générique et un
diagnostic utile : dis précisément ce que tu as changé et ce que tu redoutes.

La routine ne transporte pas de connecteurs MCP : si la session diag n'a pas
l'outil GitHub au réveil, elle publie via `gh` en ligne de commande, ou en
dernier recours pousse son rapport dans `radar-diag/reports/<sha>.md`. Sans
commentaire sur l'issue au bout d'une heure, va voir là-bas (`add_repo` sur
`alixclaudel-hue/radar-diag`) avant de conclure que la boucle est cassée.

## Comment consommer le rapport

Le rapport arrive en commentaire de l'issue et te réveille.

- **Vérifie chaque finding contre le code avant de corriger.** Le rapport est
  une observation extérieure, pas une vérité : la preuve verbatim dit ce qui se
  passe, l'interprétation peut être fausse. Une finding non reproductible dans
  le code se discute, elle ne s'applique pas les yeux fermés.
- Traite les bloquants d'abord, dans une PR dédiée, avec un test qui reproduit
  le problème constaté avant de le corriger.
- **Distingue « bug » de « état transitoire »** : si le diagnostic constate un
  crash parce que les données ne sont pas encore reconstruites, le correctif
  n'est pas forcément du code — parfois c'est juste une action à faire tourner.
  Dis-le clairement à l'utilisateur plutôt que de coder un contournement.
- Ferme l'issue quand la boucle est finie, avec une ligne de conclusion.

## Ce que tu rends à l'utilisateur

Une synthèse, pas le rapport brut : ce qui a été trouvé, ce que tu as corrigé,
ce qui reste et pourquoi, ce qu'il doit faire lui-même (les actions dans l'appli
ou sur le VPS, que ni toi ni la session diag n'avez le droit de déclencher).

## Garde-fous

- **Trois allers-retours maximum** par déploiement. Au troisième, tu escalades à
  l'utilisateur avec ce qui reste ouvert — pas un quatrième tour.
- **Délai de garde** : sans rapport au bout d'une heure, préviens l'utilisateur
  plutôt que d'attendre en silence. La session diag dépend d'un process vivant
  sur le VPS ; s'il est tombé, les réveils partent dans le vide.
- La session diag ne touche jamais au code ni aux données : si un correctif est
  nécessaire, il passe par toi, par une PR.
