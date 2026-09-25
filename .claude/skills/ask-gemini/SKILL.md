---
name: ask-gemini
description: >
  ALIAS de `delegate` — conservé pour ne casser aucune référence existante.
  La règle de délégation, les modes, les tiers et les recettes sont désormais
  décrits une seule fois dans le skill `delegate`.
---

# Skill : ask-gemini → alias de `delegate`

Ce skill n'a plus de contenu propre : il a été remplacé par
[`delegate`](../delegate/SKILL.md:1), **fournisseur-neutre** et adossé à la
passerelle multi-fournisseurs [`scripts/ai_broker.py`](../../../scripts/ai_broker.py:1).

Deux choses ont changé et méritent d'être dites ici, parce que les anciennes
recettes de ce fichier les ignoraient :

- **Le point d'entrée n'est plus Gemini seul.** `scripts/ai_query.py` reste le
  transport Gemini, conservé et testé ; il ne faut plus l'appeler directement
  pour du travail multi-fournisseurs. Passer par
  [`scripts/ai_broker.py`](../../../scripts/ai_broker.py:1), qui choisit
  gratuit d'abord (OpenRouter gratuit, Gemini) et n'atteint le payant que sur
  critère explicite.
- **Le garde-fou s'appelle désormais** [`delegation_gate`](../../../scripts/hooks/delegation_gate.py:1)
  (l'ancien nom `gemini_gate.py` subsiste comme simple adaptateur). Il bloque
  toujours commit, test et nouveau module sans reçu récent, et il signale en plus
  en télémétrie tout reçu payant non justifié.

Lire [`delegate`](../delegate/SKILL.md:1) pour la règle complète : modes, tiers,
recettes, garantie JSON, et le rappel des chemins absolus sous `~/radar-work`.
