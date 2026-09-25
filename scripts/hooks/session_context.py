#!/usr/bin/env python3
"""Hook `SessionStart` — contexte de début de session rendu déterministe (plan §8).

Le mode `context` du courtier existait mais ne servait que si le modèle décidait
de l'appeler : autrement dit, presque jamais. Ce hook lui retire cette décision.
Trois gestes, dans cet ordre :

1. il lit la **liste** des documents de contexte — un fichier,
   `docs/context-manifest.json` (surchargeable par `RADAR_CONTEXT_MANIFEST`) ;
2. il regarde ce qui est **déjà analysé** dans le cache disque
   ([`scripts/ai/context_cache.py`](../ai/context_cache.py:1)) et imprime un
   **digest compact** : état de chaque document, et un condensé des points clés
   déjà connus. Les résumés complets restent sur disque, relus à la demande par
   `python3 scripts/ai_broker.py --mode context -f <fichier>` ;
3. si des documents ne sont pas encore analysés, il lance le **pavage manquant en
   arrière-plan détaché** et rend la main immédiatement : ouvrir une session ne
   doit jamais attendre un appel réseau.

Deux règles héritées de [`telemetry.py`](telemetry.py:1), pour la même raison —
un hook de confort qui casse la session est pire qu'absent :

- **ne jamais bloquer** : le code de sortie est toujours 0, quoi qu'il arrive ;
- **ne jamais imprimer de secret** : la présence d'une clé est testée, sa valeur
  n'est jamais lue ni recopiée. Le digest ne contient que des chemins, des
  tailles et du texte déjà produit par un modèle.

Le rafraîchissement de fond est réglé par `RADAR_CONTEXT_AUTO` : `auto` (défaut)
ne le lance que si une clé fournisseur est détectée, `1`/`oui` le force, `0`/`non`
le désactive. Il ne part jamais quand il n'y a rien à paver.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

MAX_DIGEST = 4000
MAX_POINTS = 3
MAX_POINT_CHARS = 200
MAX_WHY = 160

# Le hook vit dans <dépôt>/scripts/hooks/ : deux remontées donnent la racine, qu'il
# faut sur le chemin d'import pour retrouver le socle `scripts.ai` quel que soit le
# répertoire courant de la session.
_RACINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _RACINE not in sys.path:
    sys.path.insert(0, _RACINE)

try:  # seul import lourd du hook ; sans lui, le digest reste utile mais sans cache
    from scripts.ai import context_cache
except Exception:  # pragma: no cover - dépend de l'installation
    context_cache = None  # type: ignore[assignment]

# Noms de clés reconnus. Le suffixe est libre (`OPENROUTER_API_KEY_2`, rotation),
# d'où la comparaison par préfixe plutôt que par égalité.
_CLES_ENV = (
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
)
_DESACTIVE = frozenset({"0", "no", "non", "false", "off"})
_FORCE = frozenset({"1", "yes", "oui", "true", "on"})


def _est_nom_de_cle(nom: str) -> bool:
    """`OPENROUTER_API_KEY` et ses variantes suffixées (`_2`, `_FREE`) comptent."""
    return nom in _CLES_ENV or any(nom.startswith(base + "_") for base in _CLES_ENV)


def manifest_path(project_dir: str) -> str:
    """Chemin du manifeste : `RADAR_CONTEXT_MANIFEST`, sinon `docs/` du dépôt."""
    override = (os.environ.get("RADAR_CONTEXT_MANIFEST") or "").strip()
    if override:
        return override
    return os.path.join(project_dir, "docs", "context-manifest.json")


def load_manifest(path: str) -> list[dict]:
    """Documents déclarés, dans l'ordre du fichier ; toute anomalie rend `[]`.

    Filtré plutôt que validé : une entrée sans `path` n'a pas de sens, les autres
    sont gardées — un manifeste à moitié écrit ne doit pas vider la session de son
    contexte, seulement l'écourter.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            charge = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(charge, dict):
        return []
    bruts = charge.get("documents")
    if not isinstance(bruts, list):
        return []
    documents: list[dict] = []
    for item in bruts:
        if not isinstance(item, dict):
            continue
        chemin = item.get("path")
        if not isinstance(chemin, str) or not chemin.strip():
            continue
        documents.append(
            {"path": chemin.strip(), "why": str(item.get("why") or "").strip()[:MAX_WHY]}
        )
    return documents


def _resoudre(project_dir: str, chemin: str) -> str:
    """Chemin absolu normalisé d'une entrée du manifeste."""
    if os.path.isabs(chemin):
        return os.path.normpath(chemin)
    return os.path.normpath(os.path.join(project_dir, chemin))


def _presence_cle(project_dir: str) -> bool:
    """Un fournisseur a-t-il une clé configurée ? La valeur n'est jamais lue.

    On regarde l'environnement, puis les `.env` plausibles — celui du dépôt et
    celui de `~/radar`, où vivent réellement les secrets — en se contentant de
    repérer un nom de clé suivi d'un `=`.
    """
    for nom, valeur in os.environ.items():
        if _est_nom_de_cle(nom) and (valeur or "").strip():
            return True
    candidats = (
        os.path.join(project_dir, ".env"),
        os.path.join(os.path.expanduser("~/radar"), ".env"),
    )
    for chemin in candidats:
        try:
            with open(chemin, "r", encoding="utf-8", errors="replace") as handle:
                for ligne in handle:
                    nue = ligne.strip()
                    if not nue or nue.startswith("#") or "=" not in nue:
                        continue
                    nom, _, valeur = nue.partition("=")
                    if _est_nom_de_cle(nom.strip()) and valeur.strip():
                        return True
        except OSError:
            continue
    return False


def etat_documents(resolus: list[str]) -> tuple[list[dict], list[dict], set[str]]:
    """`(paves, a_analyser, manquants)` — jamais d'exception.

    Les fichiers absents sont écartés **avant** `prepare()` : celui-ci lève sur le
    premier chemin introuvable, et un document supprimé du dépôt ne doit pas
    emporter tout le contexte de session avec lui.
    """
    presents: list[str] = []
    manquants: set[str] = set()
    for chemin in resolus:
        if os.path.isfile(chemin):
            presents.append(chemin)
        else:
            manquants.add(chemin)
    if context_cache is None:
        return [], [{"path": chemin} for chemin in presents], manquants
    try:
        contexte = context_cache.prepare(presents)
    except (FileNotFoundError, OSError):
        return [], [{"path": chemin} for chemin in presents], manquants
    documents = contexte.get("documents") or []
    paves = [doc for doc in documents if isinstance(doc, dict) and doc.get("analyse")]
    a_analyser = [doc for doc in documents if isinstance(doc, dict) and not doc.get("analyse")]
    return paves, a_analyser, manquants


def _resume_compact(analyse) -> str:
    """Condensé d'une analyse : quelques points clés, ou un repli sur les sections.

    C'est la seule partie du résumé qui entre en session ; le reste attend sur
    disque. Borné par point **et** en nombre, pour qu'un document bavard ne mange
    pas le budget des autres.
    """
    if not isinstance(analyse, dict):
        return ""
    points: list[str] = []
    for item in analyse.get("key_points") or []:
        if isinstance(item, str) and item.strip():
            points.append(item.strip()[:MAX_POINT_CHARS])
        if len(points) >= MAX_POINTS:
            break
    if not points:
        for section in analyse.get("sections") or []:
            if isinstance(section, dict) and str(section.get("summary") or "").strip():
                points.append(str(section["summary"]).strip()[:MAX_POINT_CHARS])
            if len(points) >= MAX_POINTS:
                break
    return " · ".join(points)


def _taille(chemin: str) -> str:
    try:
        octets = os.path.getsize(chemin)
    except OSError:
        return "?"
    if octets >= 1024:
        return f"{octets / 1024:.1f} ko"
    return f"{octets} o"


def lancer_rafraichissement(resolus_a_paver: list[str], journal: str) -> bool:
    """Pave en arrière-plan détaché les documents non analysés. Rend `True` si lancé.

    Détaché (`start_new_session=True`) et sans attente : la session s'ouvre quoi
    qu'il arrive. Le résultat n'est pas relu — c'est l'index de cache qui compte,
    et le prochain SessionStart en montrera l'état. La sortie part dans un journal,
    pour qu'un échec en arrière-plan soit explicable plutôt qu'invisible.
    """
    if context_cache is None or not resolus_a_paver:
        return False
    try:
        os.makedirs(os.path.dirname(journal), exist_ok=True)
    except OSError:
        return False
    commande = [
        sys.executable,
        os.path.join(_RACINE, "scripts", "ai_broker.py"),
        "--mode", "context",
        "--no-sleep",
        "-o", os.path.join(context_cache.cache_dir(), "dernier-digest.json"),
    ]
    for chemin in resolus_a_paver:
        commande += ["-f", chemin]
    try:
        with open(journal, "a", encoding="utf-8") as log:
            subprocess.Popen(
                commande,
                cwd=_RACINE,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def construire_digest(
    documents: list[dict],
    resolus: list[str],
    etat: tuple[list[dict], list[dict], set[str]],
    *,
    rafraichi: bool,
    rien_a_paver: bool,
    motif_sans_rafraichissement: str,
) -> str:
    """Texte injecté dans la session : état des documents et quelques points clés."""
    paves, a_analyser, manquants = etat
    connus = {
        os.path.normpath(doc["path"]): doc.get("analyse")
        for doc in paves
        if isinstance(doc.get("path"), str)
    }
    lignes = [
        "Contexte de session — digest automatique du mode `context` (plan §8).",
        f"Documents déclarés : {len(documents)} · analysés en cache : {len(paves)}"
        f" · à analyser : {len(a_analyser)}",
    ]
    if context_cache is not None:
        lignes.append(f"Cache disque : {context_cache.index_path()}")
    lignes.append("")
    for item, resolu in zip(documents, resolus):
        marque = (
            "manquant"
            if resolu in manquants
            else ("analysé" if os.path.normpath(resolu) in connus else "à analyser")
        )
        ligne = f"- {item['path']} — {marque} — {_taille(resolu)}"
        if item["why"]:
            ligne += f" — {item['why']}"
        lignes.append(ligne)
        resume = _resume_compact(connus.get(os.path.normpath(resolu)))
        if resume:
            lignes.append(f"    {resume}")
    lignes.append("")
    lignes.append(
        "Résumés complets en cache disque — relire avec "
        "`python3 scripts/ai_broker.py --mode context -f <fichier>`."
    )
    if rafraichi:
        lignes.append("Pavage des documents manquants lancé en arrière-plan détaché.")
    elif rien_a_paver:
        lignes.append("Cache à jour : rien à paver.")
    else:
        lignes.append(f"Aucun rafraîchissement lancé{ motif_sans_rafraichissement }.")
    texte = "\n".join(lignes)
    if len(texte) > MAX_DIGEST:
        texte = texte[:MAX_DIGEST].rstrip() + "\n[digest tronqué]"
    return texte


def main() -> int:
    try:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        documents = load_manifest(manifest_path(project_dir))
        if not documents:
            return 0
        resolus = [_resoudre(project_dir, item["path"]) for item in documents]
        etat = etat_documents(resolus)
        _, a_analyser, _ = etat
        resolus_a_paver = [
            doc["path"] for doc in a_analyser if isinstance(doc.get("path"), str)
        ]

        reglage = (os.environ.get("RADAR_CONTEXT_AUTO") or "auto").strip().lower()
        cle = _presence_cle(project_dir)
        journal = (
            os.path.join(context_cache.cache_dir(), "session-refresh.log")
            if context_cache is not None
            else ""
        )
        rafraichi = False
        motif = " (RADAR_CONTEXT_AUTO désactivé)"
        if not resolus_a_paver:
            motif = ""
        elif reglage in _DESACTIVE:
            motif = " (RADAR_CONTEXT_AUTO désactivé)"
        elif reglage in _FORCE or cle:
            rafraichi = lancer_rafraichissement(resolus_a_paver, journal)
            motif = " (lancement impossible)" if not rafraichi else ""
        else:
            motif = " (aucune clé fournisseur détectée)"

        print(
            construire_digest(
                documents,
                resolus,
                etat,
                rafraichi=rafraichi,
                rien_a_paver=not resolus_a_paver,
                motif_sans_rafraichissement=motif,
            )
        )
    except Exception:
        pass  # un hook de contexte ne doit jamais coûter la session
    return 0


if __name__ == "__main__":
    sys.exit(main())
