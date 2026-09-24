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

## Délégation Gemini — RÈGLE N°1

**Gemini passe AVANT Claude** pour le gros du travail de refactoring :

- **Scanner les duplications** : `python3 scripts/ai_query.py --mode code -f <module1> -f <module2> "Identifie les blocs de code dupliqués entre ces modules et propose une factorisation"`
- **Premier jet de la fonction factorisée** : `--mode code --check-syntax -o <fichier> "Extrais la logique commune en une fonction utilitaire : <spec>"`
- **Tests du code factorisé** : `--mode test --check-syntax -f <module> -o tests/test_<module>.py "Tests de la fonction X, cas limites compris"`
- **Pré-digérer un module volumineux** avant analyse : `--mode read -f <module>`

Claude garde le jugement architectural (quoi factoriser, où placer la nouvelle
abstraction, la relecture finale). Gemini absorbe le volume.

## Règle d'Or : DRY (Don't Repeat Yourself)
- Priorité absolue : Aucun bloc de code ne doit être dupliqué.
- Si une logique est utilisée à deux endroits, elle doit être extraite dans une fonction, un hook, ou un module utilitaire dédié.

## Méthodologie de travail
1. **Analyse avant action :** Avant de modifier ou d'ajouter du code, utilise `/ask-gemini mode read` pour pré-digérer les modules concernés, puis identifie les dépendances et les redondances potentielles.
2. **Modularité :** Favorise les petites fonctions à responsabilité unique (Single Responsibility Principle). Le premier jet peut venir de `/ask-gemini mode code`.
3. **Réutilisation :** Avant de créer une nouvelle fonction, vérifie si une fonction existante peut être adaptée pour répondre au besoin.

## Style de réponse
- Si une tâche demande de répéter une logique, explique-moi d'abord comment tu comptes la factoriser avant de générer le code final.
- Si le code existant contient déjà des répétitions, signale-le moi et propose un refactoring.
