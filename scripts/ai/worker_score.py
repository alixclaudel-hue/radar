"""Notation des ouvriers (`scripts/ai_worker.py`) : un score par chantier, une
moyenne glissante par modèle, et de quoi écarter ceux dont le résultat est mauvais.

Pourquoi : le 29/09, trois ouvriers gratuits ont épuisé leurs étapes à LIRE sans
jamais écrire (200 à 415 s perdues chacun) et rien ne retenait quel modèle s'était
comporté ainsi. Le score est calculé sur des faits du rapport (fichiers écrits,
commandes vertes), jamais sur l'avis du modèle.

Fichier `worker-scores.jsonl` à côté des reçus (même `receipts_dir()`), borné :
rotation dès `MAX_OCTETS` pour ne pas rejouer l'incident disque du pt 12.
"""
from __future__ import annotations

import json
import os
import time

NOM_FICHIER = "worker-scores.jsonl"
MAX_OCTETS = 200_000
GARDER_LIGNES = 500
FENETRE = 5            # dernières notes prises en compte par modèle
SEUIL_MAUVAIS = 35     # moyenne en dessous de laquelle un modèle est écarté
SEUIL_BON = 50         # moyenne à partir de laquelle un modèle est préféré
MIN_NOTES = 2          # pas de verdict sur un seul essai


def _dossier_recus() -> str:
    from scripts import ai_query
    return ai_query.receipts_dir()


def noter(rapport: dict) -> dict:
    """Score 0-100 et motifs. Un chantier qui n'a modifié aucun fichier plafonne à
    10 : c'est exactement le cas « lit sans écrire » qu'on veut pénaliser."""
    motifs: list[str] = []
    score = 0
    fichiers = rapport.get("fichiers_modifies") or []
    if rapport.get("statut") == "termine":
        score += 25
        motifs.append("+25 terminé")
    if fichiers:
        score += 30
        motifs.append("+30 fichiers écrits")
    if rapport.get("passent"):
        score += 20
        motifs.append("+20 commande verte")
        if not rapport.get("restent_rouges"):
            score += 10
            motifs.append("+10 rien de rouge")
    malus = min(20, 5 * (len(rapport.get("refus") or []) + len(rapport.get("erreurs") or [])))
    if malus:
        score -= malus
        motifs.append(f"-{malus} refus/erreurs")
    if rapport.get("arret") in ("etapes", "secondes", "jetons"):
        score -= 10
        motifs.append("-10 borne atteinte")
    if not fichiers:
        score = min(score, 10)
        motifs.append("plafond 10 : aucune écriture")
    return {"score": max(0, min(100, score)), "motifs": motifs}


def cle_modele(fournisseur: str | None, modele: str | None) -> str | None:
    if not modele:
        return None
    # Idempotent : `modele` porte parfois déjà le préfixe fournisseur (reçu relu
    # tel quel). Sans ce garde-fou, `worker-scores.jsonl` accumule des clés
    # doublées (« gemini:gemini:x ») qu'aucun candidat du routeur ne reconnaît
    # plus jamais — constaté le 01/10, cascade vide à chaque rotation.
    if fournisseur and modele.startswith(f"{fournisseur}:"):
        return modele
    return f"{fournisseur}:{modele}" if fournisseur else modele


def enregistrer(rapport: dict, note: dict, dossier: str | None = None) -> None:
    """Ajoute une ligne ; n'échoue jamais (la notation ne doit pas casser un chantier)."""
    try:
        chemin = os.path.join(dossier or _dossier_recus(), NOM_FICHIER)
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        ligne = {
            "ts": time.time(),
            "modele": cle_modele(rapport.get("fournisseur"), rapport.get("modele")),
            "score": note["score"],
            "statut": rapport.get("statut"),
            "arret": rapport.get("arret"),
            "etapes": rapport.get("etapes"),
            "fichiers": len(rapport.get("fichiers_modifies") or []),
        }
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        if os.path.getsize(chemin) > MAX_OCTETS:
            with open(chemin, encoding="utf-8") as f:
                reste = f.readlines()[-GARDER_LIGNES:]
            with open(chemin, "w", encoding="utf-8") as f:
                f.writelines(reste)
    except OSError:
        pass


def moyennes(dossier: str | None = None) -> dict[str, tuple[float, int]]:
    """{modèle: (moyenne des FENETRE dernières notes, nombre de notes retenues)}."""
    notes: dict[str, list[int]] = {}
    try:
        with open(os.path.join(dossier or _dossier_recus(), NOM_FICHIER), encoding="utf-8") as f:
            for brut in f:
                try:
                    d = json.loads(brut)
                except ValueError:
                    continue
                if d.get("modele") and isinstance(d.get("score"), (int, float)):
                    notes.setdefault(d["modele"], []).append(d["score"])
    except OSError:
        return {}
    return {m: (sum(v[-FENETRE:]) / len(v[-FENETRE:]), len(v[-FENETRE:])) for m, v in notes.items()}


def mauvais(dossier: str | None = None) -> set[str]:
    return {m for m, (moy, n) in moyennes(dossier).items()
            if n >= MIN_NOTES and moy < SEUIL_MAUVAIS}


def meilleur(exclus: set[str] | None = None, dossier: str | None = None) -> str | None:
    """Modèle de meilleure moyenne (>= SEUIL_BON) hors `exclus`, sinon None : le
    routeur du courtier garde alors la main."""
    exclus = exclus or set()
    bons = [(moy, m) for m, (moy, n) in moyennes(dossier).items()
            if m not in exclus and n >= MIN_NOTES and moy >= SEUIL_BON]
    return max(bons)[1] if bons else None


def dernier_modele_courtier(depuis_ts: float, dossier: str | None = None) -> tuple[str | None, str | None]:
    """(fournisseur, modèle) du dernier appel `ai_broker` du reçu depuis `depuis_ts`.
    Le rapport de l'ouvrier ne les connaissait pas (`fournisseur: null` dans tous
    les rapports du 29/09) : sans cela, impossible de noter un modèle."""
    chemin = os.path.join(dossier or _dossier_recus(), "gemini-receipts.jsonl")
    try:
        with open(chemin, encoding="utf-8") as f:
            fin = f.readlines()[-40:]
    except OSError:
        return None, None
    for brut in reversed(fin):
        try:
            d = json.loads(brut)
        except ValueError:
            continue
        if d.get("source") == "ai_broker" and d.get("ts", 0) >= depuis_ts and d.get("model"):
            return d.get("provider"), d.get("model")
    return None, None
