#!/usr/bin/env python3
"""Benchmark de vitesse homogène sur tous les modèles gratuits du panel du courtier.

Pourquoi figer le protocole : comparer des latences n'a de sens que si le travail
demandé est identique. Un seul prompt, un seul mode (code), zéro réparation
(--max-repairs 0), cache et enveloppe désactivés : sinon on mesure surtout la
configuration du courtier et non la réactivité du modèle.

La qualité est jugée par la machine (trois assertions sur is_palindrome), jamais
à l'œil, ce qui rend le classement reproductible.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PROMPT = (
    "Écris une fonction Python is_palindrome(s: str) -> bool qui ignore la casse, "
    "les espaces et la ponctuation. Réponds uniquement avec le code."
)

VERIFICATION = (
    "\n\nassert is_palindrome('A man, a plan, a canal: Panama') is True\n"
    "assert is_palindrome('hello') is False\n"
    "assert is_palindrome('') is True\n"
)

STATUT_OK = "ok"
STATUT_CODE_FAUX = "code_faux"
STATUT_ERREUR = "erreur"
STATUT_TIMEOUT = "timeout"

DUREE_MAX_LISTE = 120
DUREE_MAX_VERIFICATION = 10
TAILLE_ERREUR = 200

_BLOC_CODE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_BLOC_CODE_SANS_RETOUR = re.compile(r"```(.*?)```", re.DOTALL)

# Les messages viennent d'un service distant : on masque ce qui ressemble à un
# secret avant de l'écrire à l'écran ou sur disque.
_MASQUES = (
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{8,}"), "<secret>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{16,}"), "<secret>"),
    (re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}"), "<secret>"),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|authorization|bearer)\b\s*[:=]\s*\S+"),
        r"\1: <secret>",
    ),
)


@dataclass(frozen=True)
class Resultat:
    modele: str
    statut: str
    secondes: float
    erreur_tronquee: str = ""


def tronquer(texte: str) -> str:
    """Aplati et masque un message : on veut une trace courte et sans secret."""
    propre = " ".join((texte or "").split())
    for motif, remplacement in _MASQUES:
        propre = motif.sub(remplacement, propre)
    return propre[:TAILLE_ERREUR]


def racine_depot() -> Path:
    return Path(__file__).resolve().parent.parent


def executer(cmd, racine: Path, timeout: int):
    """Point de passage unique vers subprocess : cwd toujours la racine du dépôt."""
    return subprocess.run(
        cmd,
        cwd=str(racine),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def lister_modeles(racine: Path) -> list[str]:
    broker = racine / "scripts" / "ai_broker.py"
    proc = executer(
        [
            sys.executable,
            str(broker),
            "--list-candidates",
            "--mode",
            "general",
            "--max-candidates",
            "100",
        ],
        racine,
        DUREE_MAX_LISTE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "échec de --list-candidates : " + tronquer(proc.stderr or proc.stdout)
        )
    modeles: list[str] = []
    vus: set[str] = set()
    for ligne in proc.stdout.splitlines():
        if not ligne.startswith("[inclus] ") or "(gratuit)" not in ligne:
            continue
        jetons = ligne.split()
        if len(jetons) < 2:
            continue
        identifiant = jetons[1]
        if identifiant not in vus:
            vus.add(identifiant)
            modeles.append(identifiant)
    return modeles


def extraire_code(sortie: str) -> str:
    # Le modèle emballe presque toujours sa réponse dans un bloc Markdown : on
    # l'isole, sinon les phrases de commentaire feraient échouer l'exécution.
    bloc = _BLOC_CODE.search(sortie) or _BLOC_CODE_SANS_RETOUR.search(sortie)
    return bloc.group(1) if bloc else sortie


def verifier_code(code: str, racine: Path) -> bool:
    try:
        proc = executer(
            [sys.executable, "-c", code + VERIFICATION], racine, DUREE_MAX_VERIFICATION
        )
    except (subprocess.TimeoutExpired, OSError):
        # Une boucle infinie ou un interpréteur cassé ne vaut pas mieux qu'un échec.
        return False
    return proc.returncode == 0


def mesurer(modele: str, broker: Path, racine: Path, timeout: int) -> Resultat:
    commande = [
        sys.executable,
        str(broker),
        "--mode",
        "code",
        "--check-syntax",
        "--max-repairs",
        "0",
        "--no-cache",
        "--no-envelope",
        "-m",
        modele,
        PROMPT,
    ]
    debut = time.monotonic()
    try:
        proc = executer(commande, racine, timeout)
    except subprocess.TimeoutExpired as exc:
        return Resultat(
            modele, STATUT_TIMEOUT, time.monotonic() - debut, tronquer(str(exc))
        )
    duree = time.monotonic() - debut
    if proc.returncode != 0:
        return Resultat(
            modele, STATUT_ERREUR, duree, tronquer(proc.stderr or proc.stdout)
        )
    if verifier_code(extraire_code(proc.stdout), racine):
        return Resultat(modele, STATUT_OK, duree)
    return Resultat(
        modele, STATUT_CODE_FAUX, duree, "les assertions du test ont échoué"
    )


def cle_tri(resultat: Resultat) -> tuple[int, float]:
    # ok d'abord ; ensuite les temps croissants, y compris pour les échecs
    # (un code_faux en 4 s est une information plus utile qu'un ordre arbitraire).
    return (0 if resultat.statut == STATUT_OK else 1, resultat.secondes)


def afficher_tableau(resultats: list[Resultat]) -> None:
    largeur = max([len("Modèle")] + [len(r.modele) for r in resultats])
    entete = f"{'Rang':>4}  {'Modèle':<{largeur}}  {'Statut':<10}  {'Secondes':>8}"
    print(entete)
    print("-" * len(entete))
    for rang, resultat in enumerate(resultats, start=1):
        print(
            f"{rang:>4}  {resultat.modele:<{largeur}}  "
            f"{resultat.statut:<10}  {resultat.secondes:>8.2f}"
        )


def ecrire_json(resultats: list[Resultat], destination: str) -> None:
    chemin = Path(destination)
    if str(chemin.parent) not in ("", "."):
        chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as flux:
        json.dump([asdict(r) for r in resultats], flux, ensure_ascii=False, indent=2)
        flux.write("\n")


def parser_arguments(argv=None) -> argparse.Namespace:
    parseur = argparse.ArgumentParser(
        description="Benchmark de vitesse des modèles gratuits du courtier."
    )
    parseur.add_argument("--timeout", type=int, default=90, help="délai max par modèle (s)")
    parseur.add_argument("--workers", type=int, default=4, help="modèles testés en parallèle")
    parseur.add_argument("--out", default="./bench_speed.json", help="fichier JSON de sortie")
    parseur.add_argument("--limit", type=int, default=0, help="ne tester que les N premiers")
    return parseur.parse_args(argv)


def main(argv=None) -> int:
    args = parser_arguments(argv)
    racine = racine_depot()
    broker = racine / "scripts" / "ai_broker.py"
    if not broker.is_file():
        print(f"introuvable : {broker}", file=sys.stderr)
        return 2

    try:
        modeles = lister_modeles(racine)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"listing impossible : {tronquer(str(exc))}", file=sys.stderr)
        return 2

    if args.limit > 0:
        modeles = modeles[: args.limit]
    if not modeles:
        print("aucun modèle gratuit trouvé", file=sys.stderr)
        return 1

    workers = max(1, args.workers)
    print(
        f"{len(modeles)} modèle(s), timeout {args.timeout}s, {workers} worker(s)",
        file=sys.stderr,
    )

    resultats: list[Resultat] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futurs = {
            pool.submit(mesurer, modele, broker, racine, args.timeout): modele
            for modele in modeles
        }
        for futur in concurrent.futures.as_completed(futurs):
            modele = futurs[futur]
            try:
                resultat = futur.result()
            except Exception as exc:  # un modèle défaillant ne doit pas tuer le banc
                resultat = Resultat(modele, STATUT_ERREUR, 0.0, tronquer(str(exc)))
            resultats.append(resultat)
            print(
                f"  {modele} -> {resultat.statut} ({resultat.secondes:.1f}s)",
                file=sys.stderr,
            )

    resultats.sort(key=cle_tri)
    afficher_tableau(resultats)
    ecrire_json(resultats, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
