#!/usr/bin/env python3
"""Cache disque du mode `context` — un document n'est analysé qu'une fois.

Deux exigences du plan (§6) se répondent l'une l'autre :

1. « **Pavage de contexte** : le mode `context` accepte plusieurs documents et
   les envoie en **un seul appel** » — puisque le quota s'exprime en nombre de
   requêtes, étaler cinq fichiers sur cinq appels gaspille quatre requêtes ;
2. « cache disque indexé par empreinte du fichier, **jamais relu deux fois** ».

L'empreinte est celle du texte **tel qu'il est envoyé** (lignes numérotées),
pas celle du fichier sur disque : c'est ce qui garantit que la clé décrit
exactement ce que le modèle a vu. Une seule ligne changée change l'empreinte,
donc invalide l'entrée — sans durée de vie à deviner, et sans relire un document
que l'opérateur vient de corriger.

Comme [`quota`](quota.py:1) et [`health`](health.py:1), ce module applique la
règle « rien ici ne lève » : un index corrompu, tronqué ou illisible rend un
index vierge plutôt qu'une exception. La seule exception assumée est
`FileNotFoundError`, qui dit à l'appelant qu'un document demandé n'existe pas —
c'est une erreur de l'opérateur, pas une panne d'état.

L'index vit sous le dossier d'usage (`RADAR_AI_USAGE_DIR`), à côté du ledger :
un bac à sable qui isole le ledger isole donc le cache du même coup, ce dont le
banc [`scripts/bench_ai_broker.py`](../bench_ai_broker.py:1) se sert pour
observer la persistance entre deux exécutions.
"""

from __future__ import annotations

import hashlib
import json
import os

from scripts.ai import prompts, quota

INDEX_NAME = "context_index.json"
INDEX_VERSION = 1


def cache_dir() -> str:
    """Dossier de l'index de contexte.

    `RADAR_AI_CONTEXT_CACHE` prime : il sert au banc et aux essais manuels qui
    veulent un cache distinct du ledger. Sinon on se range sous `usage_dir()`,
    pour qu'une seule variable isole tout l'état d'un essai.
    """
    explicite = os.environ.get("RADAR_AI_CONTEXT_CACHE", "").strip()
    if explicite:
        return explicite
    return os.path.join(quota.usage_dir(), "context")


def index_path(directory: str | None = None) -> str:
    """Chemin du fichier d'index, sous `directory` ou sous `cache_dir()`."""
    return os.path.join(directory or cache_dir(), INDEX_NAME)


def empreinte(texte: str) -> str:
    """Empreinte courte et stable du texte envoyé.

    Seize caractères hexadécimaux : assez pour qu'une collision soit hors de
    portée sur un corpus de documents, assez court pour qu'un index reste
    lisible à l'œil quand on le dépanne.
    """
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()[:16]


def cle_document(chemin: str, envoi: str) -> str:
    """Clé d'une analyse : le chemin absolu **et** l'empreinte du texte envoyé.

    Les deux ensemble, parce qu'aucun ne suffit : l'empreinte seule confondrait
    deux fichiers identiques qu'il faut pourtant nommer séparément dans la
    sortie ; le chemin seul resservirait une analyse périmée après une
    correction du fichier.
    """
    return f"{os.path.abspath(chemin)}|{empreinte(envoi)}"


def _index_vierge() -> dict:
    return {"version": INDEX_VERSION, "documents": {}}


def load_index(path: str | None = None) -> dict:
    """Index de contexte ; toute anomalie rend un index vierge.

    Une version inconnue rend un index vierge : mieux vaut réanalyser une fois
    que d'interpréter une structure écrite par une version future.
    """
    try:
        with open(path or index_path(), "r", encoding="utf-8") as handle:
            charge = json.load(handle)
    except (OSError, ValueError):
        return _index_vierge()
    if not isinstance(charge, dict):
        return _index_vierge()
    if charge.get("version") != INDEX_VERSION:
        return _index_vierge()
    if not isinstance(charge.get("documents"), dict):
        return _index_vierge()
    return charge


def save_index(index: dict, path: str | None = None) -> bool:
    """Écriture atomique (`tmp` puis `replace`) ; n'échoue jamais bruyamment."""
    destination = path or index_path()
    try:
        dossier = os.path.dirname(destination)
        if dossier:
            os.makedirs(dossier, exist_ok=True)
        temporaire = f"{destination}.tmp"
        with open(temporaire, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
        os.replace(temporaire, destination)
        return True
    except OSError:
        return False


def _document(chemin: str, envoi: str, analyse) -> dict:
    return {
        "path": chemin,
        "cle": cle_document(chemin, envoi),
        "envoi": envoi,
        "analyse": analyse,
    }


def prepare(
    fichiers: list[str] | None,
    *,
    rafraichir: bool = False,
    index: dict | None = None,
) -> dict:
    """Prépare un pavage : ce qui est déjà analysé, ce qu'il reste à envoyer.

    `fichiers` est la liste des chemins **tels que l'opérateur les a donnés** :
    on garde cette écriture pour l'en-tête `--- Fichier : ... ---` et pour la
    sortie, afin que ce que l'appelant relit corresponde à ce qu'il a tapé.

    `rafraichir=True` (le `--no-cache` de la CLI) ignore l'index : tous les
    documents repartent en analyse, et l'appelant ne stockera rien. C'est ce qui
    rend l'option utile quand on se méfie d'un index.

    Lève `FileNotFoundError` sur un chemin absent — le seul échec que l'appelant
    doit traduire en message d'erreur, comme le faisait la construction du
    prompt historique.
    """
    etat = index if index is not None else load_index()
    connus = etat.get("documents") or {}

    documents: list[dict] = []
    a_analyser: list[dict] = []
    for chemin in fichiers or []:
        if not os.path.isfile(chemin):
            raise FileNotFoundError(chemin)
        with open(chemin, "r", encoding="utf-8", errors="replace") as handle:
            brut = handle.read()
        envoi = prompts.numbered_lines(brut)
        entree = (connus.get(cle_document(chemin, envoi)) or {}) if not rafraichir else {}
        analyse = entree.get("analyse") if isinstance(entree, dict) else None
        document = _document(chemin, envoi, analyse if isinstance(analyse, dict) else None)
        documents.append(document)
        if document["analyse"] is None:
            a_analyser.append(document)

    return {
        "documents": documents,
        "a_analyser": a_analyser,
        "index": etat,
        "paves": len(documents) - len(a_analyser),
    }


def _normalise(chemin: str) -> str:
    """Forme comparable d'un chemin : absolu, sans `..` ni `./` résiduels."""
    return os.path.normpath(os.path.abspath(str(chemin)))


def _meme_chemin(demande: str, rendu: str) -> bool:
    """Le chemin rendu par le modèle désigne-t-il le document demandé ?

    On accepte l'égalité après normalisation, et à défaut l'égalité du seul nom
    de fichier : un modèle qui raccourcit `docs/a/b.md` en `b.md` désigne sans
    ambiguïté le même document dès lors que les noms de base sont distincts.
    """
    if _normalise(demande) == _normalise(rendu):
        return True
    return os.path.basename(demande) == os.path.basename(rendu)


def apparier(charge, documents: list[dict]) -> list[dict] | None:
    """Range les analyses rendues par le modèle dans l'ordre des documents.

    Deux stratégies, dans cet ordre :

    1. **par le chemin** que le modèle a recopié — c'est ce qui survit à une
       réponse donnée dans le désordre ;
    2. **par la position**, mais uniquement si *aucun* chemin n'a été reconnu :
       une réponse sans `path` du tout est le cas des modèles qui suivent le
       schéma à moitié.

    Un appariement **partiel** est refusé (`None`) : mieux vaut un échec explicite
    qu'une analyse classée sous le mauvais document, qui empoisonnerait en
    silence le résumé de session. Le champ `path` de chaque analyse est réécrit
    avec le chemin demandé, pour que la sortie soit celle de la ligne de commande.
    """
    if not isinstance(charge, dict):
        return None
    bruts = charge.get("documents")
    if not isinstance(bruts, list) or not bruts:
        return None
    if len(bruts) != len(documents):
        return None

    analyses = [item if isinstance(item, dict) else {} for item in bruts]
    associes: dict[int, dict] = {}
    reconnus = 0
    for item in analyses:
        rendu = item.get("path")
        if not isinstance(rendu, str) or not rendu.strip():
            continue
        cible = next(
            (i for i, doc in enumerate(documents) if _meme_chemin(doc["path"], rendu)),
            None,
        )
        if cible is None or cible in associes:
            continue
        associes[cible] = item
        reconnus += 1

    if reconnus == 0:
        associes = {i: item for i, item in enumerate(analyses)}
    elif reconnus != len(documents):
        return None

    resultat: list[dict] = []
    for index, doc in enumerate(documents):
        item = associes.get(index)
        if item is None:
            return None
        resultat.append(dict(item, path=doc["path"]))
    return resultat


def store(
    contexte: dict,
    analyses: list[dict],
    *,
    model: str | None = None,
    source: str | None = None,
    ts: float | None = None,
) -> dict:
    """Range les analyses dans l'index et les attache au contexte en mémoire.

    `analyses` est aligné sur `contexte["a_analyser"]`, la liste des documents
    **envoyés** : les documents déjà servis par le cache n'ont pas été soumis au
    modèle et n'ont donc rien à recevoir ici.

    L'index est réécrit en une fois à la fin : le pavage est atomique du point de
    vue du cache, donc une interruption ne laisse pas la moitié des documents
    indexés et l'autre moitié à refaire silencieusement.
    """
    index = contexte.get("index") or _index_vierge()
    connus = index.setdefault("documents", {})
    for document, analyse in zip(contexte["a_analyser"], analyses):
        document["analyse"] = analyse
        connus[document["cle"]] = {
            "path": document["path"],
            "analyse": analyse,
            "model": model,
            "source": source,
            "ts": ts,
        }
    save_index(index)
    return index


def assembler(documents: list[dict]) -> str:
    """Sortie du mode `context` : un objet JSON, un élément par document.

    L'ordre est celui de la ligne de commande, jamais celui de la réponse : c'est
    le contrat que l'appelant lit, et il ne doit pas dépendre de l'humeur d'un
    modèle. Indenté, parce qu'un digest de contexte se relit à l'œil.
    """
    charge = {"documents": [doc["analyse"] or {} for doc in documents]}
    return json.dumps(charge, ensure_ascii=False, indent=2)


__all__ = [
    "INDEX_NAME",
    "INDEX_VERSION",
    "apparier",
    "assembler",
    "cache_dir",
    "cle_document",
    "empreinte",
    "index_path",
    "load_index",
    "prepare",
    "save_index",
    "store",
]
