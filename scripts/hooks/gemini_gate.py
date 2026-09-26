"""Alias de compatibilité — le hook vit désormais dans `delegation_gate.py`.

Le nom `gemini_gate` ne décrivait plus le périmètre : le garde-fou vaut pour
TOUT fournisseur (Gemini natif comme OpenRouter/DeepSeek/xAI via le courtier).
`delegation_gate.py` porte l'implémentation ; ce fichier n'existe que pour ne
casser aucune référence existante (tests, registre de la boucle, anciens
`settings.json` hors dépôt). Il réexporte l'API publique à l'identique et reste
exécutable : `python3 scripts/hooks/gemini_gate.py` se comporte exactement comme
avant le renommage.

Ne rien ajouter ici : toute évolution se fait dans `delegation_gate.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CHEMIN = Path(__file__).resolve().parent / "delegation_gate.py"

_spec = importlib.util.spec_from_file_location("delegation_gate", _CHEMIN)
if _spec is None or _spec.loader is None:  # pragma: no cover - garde-fou d'import
    raise ImportError(f"Impossible de charger {_CHEMIN}")

_module = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("delegation_gate", _module)
_spec.loader.exec_module(_module)

# API publique réexportée (blocage historique + signalement non bloquant).
GATE_TTL = _module.GATE_TTL
GATE_MIN_PROMPT_CHARS = _module.GATE_MIN_PROMPT_CHARS
PAID_JUSTIFICATION_MODES = _module.PAID_JUSTIFICATION_MODES
PAID_TIERS = _module.PAID_TIERS
SIGNAL_KIND = _module.SIGNAL_KIND
SEEN_FILE = _module.SEEN_FILE
load_receipts = _module.load_receipts
has_recent_receipt = _module.has_recent_receipt
receipts_path_for = _module.receipts_path_for
required_modes = _module.required_modes
paid_without_reason = _module.paid_without_reason
signal_unjustified_paid = _module.signal_unjustified_paid
main = _module.main


if __name__ == "__main__":
    sys.exit(main())
