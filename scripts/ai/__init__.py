"""Socle de la passerelle multi-fournisseurs (plan §4).

Modules :

- [`redact`](redact.py:1) — masquage des secrets avant tout envoi sortant.
- [`prompts`](prompts.py:1) — rôles par mode, tier associé, extraction de code.
- [`jsonout`](jsonout.py:1) — lecture d'un objet JSON dans une réponse de
  modèle : un seul décodeur, partagé par l'enveloppe de code et le contexte.
- [`providers`](providers.py:1) — adaptateurs OpenAI-compatibles (OpenRouter,
  DeepSeek, xAI) et adaptateur Gemini (qui réutilise le transport déjà testé de
  [`scripts/ai_query.py`](../ai_query.py:1)).
- [`catalogue`](catalogue.py:1) — lecture du catalogue engendré
  `config/ai_models.json`.
- [`quota`](quota.py:1) — ledger d'usage, rotation de clés, plafond payant,
  écriture des reçus.
- [`context_cache`](context_cache.py:1) — index disque du mode `context` et
  pavage multi-documents.
- [`locate`](locate.py:1) — recherche locale bornée du mode `search` : classement
  déterministe et détection d'ambiguïté, sans aucun accès réseau.
- [`health`](health.py:1) — disjoncteur par (fournisseur, modèle).
- [`router`](router.py:1) — liste ordonnée de candidats, gratuit d'abord.

Aucun de ces modules n'ouvre de connexion réseau ni ne lit de fichier à
l'import : tout est fonction pure ou presque, donc testable hors ligne, ce que
le banc [`scripts/bench_ai_broker.py`](../bench_ai_broker.py:1) exploite.
"""

__all__ = [
    "catalogue",
    "context_cache",
    "health",
    "jsonout",
    "locate",
    "prompts",
    "providers",
    "quota",
    "redact",
    "router",
]
