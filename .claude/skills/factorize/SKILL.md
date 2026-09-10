---
name: factorize
description: >
  Refactoring et principe DRY (Don't Repeat Yourself) : repérer les duplications
  de code et les extraire en fonctions/hooks/modules réutilisables avant
  d'ajouter du nouveau code. Utiliser pour /factorize, "factorise", "refactor",
  "DRY", "évite la duplication", ou avant toute tâche qui répéterait une
  logique déjà présente ailleurs dans le code.
---

# Instructions de Développement - Refactoring & DRY

## Règle d'Or : DRY (Don't Repeat Yourself)
- Priorité absolue : Aucun bloc de code ne doit être dupliqué.
- Si une logique est utilisée à deux endroits, elle doit être extraite dans une fonction, un hook, ou un module utilitaire dédié.

## Méthodologie de travail
1. **Analyse avant action :** Avant de modifier ou d'ajouter du code, identifie les dépendances et les redondances potentielles.
2. **Modularité :** Favorise les petites fonctions à responsabilité unique (Single Responsibility Principle).
3. **Réutilisation :** Avant de créer une nouvelle fonction, vérifie si une fonction existante peut être adaptée pour répondre au besoin.

## Style de réponse
- Si une tâche demande de répéter une logique, explique-moi d'abord comment tu comptes la factoriser avant de générer le code final.
- Si le code existant contient déjà des répétitions, signale-le moi et propose un refactoring.
