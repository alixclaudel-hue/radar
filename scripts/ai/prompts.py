#!/usr/bin/env python3
"""Rôles par mode, tier associé et extraction de sortie — une seule source.

Les modes historiques (`code`, `test`, `diag`, `read`, `summary`, `pr`,
`general`) et leurs prompts vivent dans [`scripts/ai_query.py`](../ai_query.py:1),
et ils y restent : ce module les **importe**, il ne les recopie pas. Une
correction de prompt doit avoir un seul endroit où exister, sinon les deux
copies divergent en une semaine.

Ce que ce module ajoute :

- les modes propres à la passerelle multi-fournisseurs (`reasoning`, `context`,
  `search`), qui n'ont pas de sens dans un appel mono-Gemini — le premier parce
  qu'il vise un fournisseur payant choisi explicitement, le deuxième parce qu'il
  pave plusieurs documents en un seul appel et s'appuie sur un cache disque, le
  troisième parce qu'il répond d'abord avec le seul disque et ne sollicite un
  modèle que pour départager des candidats locaux ;
- la résolution du tier à partir du mode, y compris pour ces modes nouveaux ;
- un point d'import unique pour le reste du socle, de sorte que le broker ne
  dépende que de ce module et non de l'implémentation Gemini.
"""

from __future__ import annotations

from scripts.ai_query import (  # noqa: F401  (réexports volontaires)
    CODE_MODES,
    SYSTEM_PROMPTS,
    extract_raw_code,
    numbered_lines,
    resolve_tier,
)

__all__ = [
    "CODE_MODES",
    "CODE_LIKE_MODES",
    "REASONING_MODES",
    "SYSTEM_PROMPTS",
    "extract_raw_code",
    "mode_choices",
    "numbered_lines",
    "resolve_mode_tier",
    "system_prompt",
]

# Modes dont la réponse attendue est du code Python : l'extraction des blocs
# markdown y est systématique, sinon un fichier écrit par `-o` serait inutilisable.
CODE_LIKE_MODES = frozenset(CODE_MODES)

# Modes de raisonnement : aucun n'est du code, mais tous acceptent d'emblée un
# fournisseur payant si l'opérateur l'a autorisé — c'est le seul cas où le
# payant n'est pas une escalade consécutive à un échec.
REASONING_MODES = frozenset({"reasoning"})

_EXTRA_PROMPTS: dict[str, str] = {
    "context": (
        "Tu es un agent expert en analyse documentaire pour le projet Radar, "
        "y compris sur des documents d'architecture ou de sécurité. "
        "Plusieurs documents te sont fournis, chacun introduit par une ligne "
        "« --- Fichier : <chemin> --- » et précédé d'un préfixe numéroté sur "
        "chaque ligne. "
        "Réponds par un **unique objet JSON**, sans texte avant ni après, de la "
        "forme {\"documents\": [ ... ]}. "
        "Le tableau contient **un élément par document fourni, dans l'ordre où "
        "ils t'ont été donnés**, aucun de plus, aucun de moins, chacun avec "
        "exactement ces clés : "
        "'path' (le chemin tel qu'il figure dans l'en-tête « --- Fichier : ... --- »), "
        "'title' (sujet ou titre principal du document), "
        "'sections' (liste d'objets {\"heading\", \"summary\"} pour chaque section majeure), "
        "'key_points' (liste de 5 à 10 points clés les plus importants), "
        "'todos' (liste des actions en attente, liste vide si aucune), "
        "'dependencies' (fichiers, modules ou systèmes externes mentionnés), "
        "'uncertain' (liste d'objets {\"location\", \"question\"} signalant chaque "
        "passage où TU N'ES PAS SÛR de ta lecture -- nuance de sécurité, "
        "contrainte d'architecture, information ambiguë ou contradictoire. "
        "'location' doit être le numéro de ligne exact tel qu'il apparaît dans le "
        "préfixe numéroté, sinon une citation exacte et courte du passage. Ne vide "
        "ce champ que si le document est trivial : un doute sincère non signalé "
        "coûte plus cher qu'un faux positif, car personne d'autre ne relira ce "
        "passage-là). "
        "N'invente aucune donnée qu'un document ne contient pas : si un document "
        "est illisible ou vide, dis-le dans son 'title' plutôt que de le combler. "
        "Ne recopie pas les documents dans ta réponse, analyse-les."
    ),
    "search": (
        "Tu es un assistant de navigation dans un dépôt de code. Un parcours "
        "local a déjà trouvé et classé des fichiers candidats ; ils te sont "
        "donnés avec, pour chacun, l'étage de correspondance et la ligne citée "
        "quand il y en a une. "
        "Ta seule tâche est de désigner le fichier qui répond le mieux à la "
        "requête. Tu ne parcours pas le dépôt, tu ne proposes aucun chemin qui ne "
        "soit pas dans la liste fournie. "
        "Réponds par un **unique objet JSON**, sans texte avant ni après, de la "
        "forme {\"path\": \"<chemin copié exactement depuis la liste>\", "
        "\"reason\": \"<une phrase qui justifie le choix>\"}. "
        "Si aucun candidat ne convient, rends {\"path\": \"\", \"reason\": "
        "\"<pourquoi aucun ne convient>\"} plutôt qu'un chemin inventé."
    ),
    "reasoning": (
        "Tu es un analyste technique senior. On te soumet une question qui demande "
        "du raisonnement : choix d'architecture, arbitrage entre deux approches, "
        "analyse d'un compromis, revue de conception. "
        "Réponds en français, en structurant explicitement : "
        "1. CE QUE DIT LE MATÉRIAU FOURNI (faits, chiffres, contraintes observables). "
        "2. HYPOTHÈSES que tu dois poser faute d'information, avec la plus faible "
        "possible et pourquoi. "
        "3. OPTIONS ENVISAGEABLES, chacune avec son coût et son risque. "
        "4. RECOMMANDATION unique et argumentée, avec le point qui la ferait changer "
        "d'avis. "
        "Sois concis : pas de reformulation de la question, pas de conclusion "
        "récapitulative. Si le matériau ne permet pas de trancher, dis-le au lieu "
        "d'inventer une donnée."
    ),
}


def mode_choices() -> list[str]:
    """Modes acceptés par la CLI, triés (superset strict de ceux d'`ai_query`)."""
    return sorted(set(SYSTEM_PROMPTS) | set(_EXTRA_PROMPTS))


def system_prompt(mode: str) -> str:
    """Instruction système du mode, ou chaîne vide si le mode est `general`."""
    return _EXTRA_PROMPTS.get(mode) or SYSTEM_PROMPTS.get(mode, "")


def resolve_mode_tier(mode: str, explicit_tier: str | None = None) -> str:
    """Tier d'un mode : le code, les tests et le raisonnement demandent `heavy`.

    Un `--tier` explicite prime toujours : l'appelant qui force un tier sait ce
    qu'il fait, et lui substituer un autre en silence fausserait les mesures.
    """
    if explicit_tier:
        return explicit_tier
    if mode in REASONING_MODES:
        return "heavy"
    return resolve_tier(mode)
