#!/usr/bin/env python3
"""Banc d'essai des modèles GRATUITS du courtier : vitesse murale et qualité vérifiée.

Pourquoi ce script : le choix d'un modèle « ouvrier » (boucle d'outils) doit
reposer sur des mesures reproductibles plutôt que sur un jugement LLM, d'où des
notes automatiques obtenues en exécutant réellement le code produit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Le script vit dans scripts/ : la racine du dépôt est le parent de son dossier.
RACINE = Path(__file__).resolve().parent.parent
BROKER = RACINE / "scripts" / "ai_broker.py"
CONFIG = RACINE / "config" / "ai_models.json"

CONSIGNE_BUG = (
    "Tu es un ouvrier de code. Corrige le bug de cette fonction. "
    "Réponds par UN SEUL bloc de code python contenant la fonction corrigée complète, "
    "puis une phrase d'explication. Maximum 500 jetons."
)

# Le bug est volontaire : la branche « doublon » ajoute l'élément au lieu de l'ignorer.
CODE_BUG = (
    "def dedupe_keep_order(items, key=None):\n"
    '    """Retire les doublons en gardant le premier rencontré ; key extrait la clé de comparaison."""\n'
    "    seen = set()\n"
    "    out = []\n"
    "    for it in items:\n"
    "        k = key(it) if key else it\n"
    "        if k in seen:\n"
    "            out.append(it)\n"
    "        else:\n"
    "            seen.add(k)\n"
    "    return out\n"
)

PROMPT_1 = CONSIGNE_BUG + "\n\n" + CODE_BUG

CONSIGNE_JSON = (
    "Tu es un ouvrier. Outils disponibles : edit_file(path, old, new) qui remplace "
    "UNE occurrence exacte de old par new. Réponds UNIQUEMENT par un objet JSON "
    '{"action":"edit","path":...,"old":...,"new":...,"commentaire":...}. '
    "Maximum 500 jetons. Tâche : ajoute à la fonction total un paramètre remise=0.0 "
    "(fraction entre 0 et 1) appliqué au sous-total avant la TVA. "
    "Contenu de radar_jobs/exemple.py :"
)

FICHIER = (
    "def total(prix, tva=0.2):\n"
    '    """Somme des prix TTC, arrondie à 2 décimales."""\n'
    "    return round(sum(prix) * (1 + tva), 2)\n"
)

PROMPT_2 = CONSIGNE_JSON + "\n" + FICHIER

ASSERTS_BUG = [
    "assert dedupe_keep_order([1, 2, 1, 3, 2]) == [1, 2, 3]",
    "assert dedupe_keep_order(['a', 'B', 'A', 'b'], key=lambda s: s.lower()) == ['a', 'B']",
    "assert dedupe_keep_order([]) == []",
    "assert dedupe_keep_order([3, 3, 3]) == [3]",
]

ASSERTS_JSON = [
    "assert total([10, 10]) == 24.0",
    "assert total([10, 10], 0.2, remise=0.5) == 12.0",
]

_MOTIF_LANGUE = re.compile(r'^\s*[A-Za-z][A-Za-z0-9+#._-]*\s*$')


def tronquer(texte: str, largeur: int) -> str:
    return texte if len(texte) <= largeur else texte[: largeur - 1] + "…"


def nom_court(ident: str) -> str:
    """Affiche le modèle sans le préfixe du fournisseur, mais avec sa lettre (o/g)."""
    fournisseur, _, modele = ident.partition(":")
    lettre = {"openrouter": "o", "gemini": "g"}.get(fournisseur, (fournisseur[:1] or "?").lower())
    return "%s %s" % (lettre, modele)


def ids_bloques() -> set[str]:
    """blocked_ids de la policy : inutile de gaspiller des appels sur des modèles écartés."""
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    policy = config.get("policy") or {}
    bruts = policy.get("blocked_ids") or []
    return {element for element in bruts if isinstance(element, str)}


def lister_modeles() -> list[str]:
    """Récupère les identifiants gratuits, dédoublonnés et filtrés par la policy."""
    commande = [
        sys.executable,
        str(BROKER),
        "--list-candidates",
        "--mode",
        "general",
        "--max-candidates",
        "100",
    ]
    proc = subprocess.run(
        commande,
        cwd=str(RACINE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "ai_broker --list-candidates a échoué (%s)" % (proc.stderr or "").strip()[:200]
        )

    bloques = ids_bloques()
    vus: set[str] = set()
    retenus: list[str] = []
    for ligne in (proc.stdout or "").splitlines():
        if "(gratuit)" not in ligne:
            continue
        correspondance = re.match(r'\s*\[inclus\]\s+(\S+)', ligne)
        if not correspondance:
            continue
        ident = correspondance.group(1)
        if ":" not in ident:
            continue
        # La policy référence le modèle sans son fournisseur.
        if ident.split(":", 1)[1] in bloques or ident in vus:
            continue
        vus.add(ident)
        retenus.append(ident)
    return retenus


def appeler_broker(ident: str, prompt: str, json_output: bool, timeout: float) -> dict:
    """Un appel = une mesure ; on isole tout ce qui peut échouer pour ne pas fausser la note."""
    commande = [sys.executable, str(BROKER), "--mode", "general", "--no-cache", "-m", ident]
    if json_output:
        commande.append("--json-output")
    commande.append(prompt)

    debut = time.monotonic()
    try:
        proc = subprocess.run(
            commande,
            cwd=str(RACINE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "temps_s": time.monotonic() - debut,
            "rc": None,
            "sortie": "",
            "erreur": "timeout %.0fs" % timeout,
            "echec": True,
            "timeout": True,
        }
    except OSError as exc:
        return {
            "temps_s": time.monotonic() - debut,
            "rc": None,
            "sortie": "",
            "erreur": ("OSError: %s" % exc)[:120],
            "echec": True,
            "timeout": False,
        }

    temps = time.monotonic() - debut
    # Troncature : garde-fou mémoire, les prompts imposent déjà 500 jetons de réponse.
    return {
        "temps_s": temps,
        "rc": proc.returncode,
        "sortie": (proc.stdout or "")[:4000],
        "erreur": (proc.stderr or "").strip()[:120],
        "echec": proc.returncode != 0,
        "timeout": False,
    }


def executer_verifications(code: str, asserts: list[str], marqueur: str) -> list[bool]:
    """Chaque assert dans son propre try : un plantage n'annule pas les points des autres."""
    lignes = [code.rstrip("\n"), ""]
    for index, assertion in enumerate(asserts):
        lignes += [
            "try:",
            "    " + assertion,
            '    print("__%s_OK_%d__")' % (marqueur, index),
            "except Exception:",
            '    print("__%s_KO_%d__")' % (marqueur, index),
        ]
    script = "\n".join(lignes) + "\n"

    try:
        proc = subprocess.run(
            [sys.executable, "-"],
            input=script,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            cwd=str(RACINE),
        )
    except Exception:
        return [False] * len(asserts)

    sortie = proc.stdout or ""
    return [("__%s_OK_%d__" % (marqueur, index)) in sortie for index in range(len(asserts))]


def extraire_bloc_code(texte: str) -> str:
    """Premier bloc ``` ... ``` ; à défaut toute la sortie (le modèle a désobéi)."""
    debut = texte.find("```")
    if debut == -1:
        return texte.strip("\n")
    reste = texte[debut + 3 :]
    fin = reste.find("```")
    interieur = reste[:fin] if fin != -1 else reste
    lignes = interieur.splitlines()
    if lignes and _MOTIF_LANGUE.match(lignes[0]):
        # Première ligne = étiquette de langage (```python) : elle casserait compile().
        interieur = "\n".join(lignes[1:])
    return interieur.strip("\n")


def noter_bug(sortie: str) -> tuple[float, str]:
    code = extraire_bloc_code(sortie)
    try:
        compile(code, "<reponse>", "exec")
    except Exception as exc:
        return 0.0, ("syntaxe: %s" % exc)[:120]

    resultats = executer_verifications(code, ASSERTS_BUG, "BUG")
    reussis = sum(1 for ok in resultats if ok)
    return 5.0 * reussis, "%d/4 asserts" % reussis


def noter_json(sortie: str) -> tuple[float, str]:
    debut, fin = sortie.find("{"), sortie.rfind("}")
    if debut == -1 or fin <= debut:
        return 0.0, "aucun objet JSON"
    try:
        objet = json.loads(sortie[debut : fin + 1])
    except json.JSONDecodeError as exc:
        return 0.0, ("JSON invalide: %s" % exc)[:120]
    if not isinstance(objet, dict):
        return 0.0, "JSON non-objet"

    note = 4.0
    if objet.get("action") != "edit" or objet.get("path") != "radar_jobs/exemple.py":
        return note, "action ou path incorrects"
    note += 4.0

    ancien, nouveau = objet.get("old"), objet.get("new")
    if not isinstance(ancien, str) or not isinstance(nouveau, str) or FICHIER.count(ancien) != 1:
        return note, "old absent ou non unique"
    note += 4.0

    patche = FICHIER.replace(ancien, nouveau, 1)
    try:
        compile(patche, "<patch>", "exec")
    except Exception as exc:
        return note, ("patch non compilable: %s" % exc)[:120]
    note += 4.0

    resultats = executer_verifications(patche, ASSERTS_JSON, "JSON")
    bon = sum(1 for ok in resultats if ok)
    note += 2.0 * bon
    return note, "%d/2 comportements" % bon


TESTS = (
    ("bug", PROMPT_1, False, noter_bug),
    ("json", PROMPT_2, True, noter_json),
)


def evaluer_modele(ident: str, timeout: float) -> dict:
    """Les 2 tests d'un même modèle tournent à la suite : on mesure un ouvrier, pas un cache."""
    details: dict[str, dict] = {}
    for nom, prompt, json_output, notation in TESTS:
        appel = appeler_broker(ident, prompt, json_output, timeout)
        if appel["echec"]:
            note, motif = 0.0, appel["erreur"] or ("code retour %s" % appel["rc"])
        else:
            try:
                note, motif = notation(appel["sortie"])
            except Exception as exc:
                # Une notation qui plante ne doit pas faire tomber tout le banc.
                note, motif = 0.0, "notation: %s" % exc
        details[nom] = {
            "temps_s": round(appel["temps_s"], 3),
            "note": float(note),
            "rc": appel["rc"],
            "timeout": bool(appel["timeout"]),
            "erreur": (motif or "")[:120],
        }

    temps = [detail["temps_s"] for detail in details.values()]
    notes = [detail["note"] for detail in details.values()]
    # Indisponible seulement si AUCUN des deux appels n'a abouti.
    indisponible = all(detail["timeout"] or detail["rc"] != 0 for detail in details.values())
    cause = " | ".join(detail["erreur"] for detail in details.values() if detail["erreur"])[:80]
    return {
        "modele": ident,
        "tests": details,
        "t_moy": sum(temps) / len(temps),
        "q_moy": sum(notes) / len(notes),
        "indispo": indisponible,
        "cause": cause or "appels en échec",
    }


def classer(resultats: list[dict]) -> tuple[list[dict], list[dict]]:
    classes = [resultat for resultat in resultats if not resultat["indispo"]]
    indisponibles = [resultat for resultat in resultats if resultat["indispo"]]
    if classes:
        temps_min = min(resultat["t_moy"] for resultat in classes)
        qualite_max = max(resultat["q_moy"] for resultat in classes)
        for resultat in classes:
            resultat["note_vitesse"] = (
                20.0 * temps_min / resultat["t_moy"] if resultat["t_moy"] > 0 else 0.0
            )
            resultat["note_qualite"] = (
                20.0 * resultat["q_moy"] / qualite_max if qualite_max > 0 else 0.0
            )
            # La qualité pèse 2× la vitesse : un ouvrier lent reste utile, un ouvrier faux non.
            resultat["note_finale"] = round(
                (resultat["note_vitesse"] + 2 * resultat["note_qualite"]) / 3, 1
            )
        classes.sort(key=lambda resultat: (-resultat["note_finale"], resultat["t_moy"]))
    return classes, indisponibles


def afficher(classes: list[dict], indisponibles: list[dict]) -> None:
    entete = "%4s  %-46s %8s %6s %7s %8s %7s" % (
        "Rang",
        "Modèle",
        "t_moy(s)",
        "Q/20",
        "Vit/20",
        "Qual/20",
        "FINAL",
    )
    print()
    print(entete)
    print("-" * len(entete))
    for rang, resultat in enumerate(classes, 1):
        print(
            "%4d  %-46s %8.2f %6.1f %7.1f %8.1f %7.1f"
            % (
                rang,
                tronquer(nom_court(resultat["modele"]), 46),
                resultat["t_moy"],
                resultat["q_moy"],
                resultat["note_vitesse"],
                resultat["note_qualite"],
                resultat["note_finale"],
            )
        )

    print()
    if indisponibles:
        print("Indisponibles :")
        for resultat in sorted(indisponibles, key=lambda element: element["modele"]):
            print(
                "  %-46s %s"
                % (tronquer(nom_court(resultat["modele"]), 46), resultat["cause"][:80])
            )
    else:
        print("Aucun modèle indisponible.")


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(
        description="Compare les modèles gratuits du courtier : vitesse et qualité."
    )
    parseur.add_argument("--timeout", type=float, default=90.0, help="délai maximum par appel broker (s)")
    parseur.add_argument("--workers", type=int, default=3, help="modèles évalués en parallèle")
    parseur.add_argument("--limit", type=int, default=0, help="ne tester que les N premiers modèles")
    parseur.add_argument("--out", default="./bench_models.json", help="fichier JSON de restitution")
    args = parseur.parse_args(argv)

    try:
        modeles = lister_modeles()
    except Exception as exc:
        print("Liste des candidats indisponible : %s" % exc, file=sys.stderr)
        return 2

    if args.limit > 0:
        modeles = modeles[: args.limit]
    if not modeles:
        print("Aucun modèle gratuit retenu.", file=sys.stderr)
        return 2

    print(
        "%d modèle(s) gratuit(s) — timeout %.0fs, %d worker(s)"
        % (len(modeles), args.timeout, args.workers)
    )

    resultats: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futurs = {pool.submit(evaluer_modele, ident, args.timeout): ident for ident in modeles}
        for futur in as_completed(futurs):
            ident = futurs[futur]
            try:
                resultat = futur.result()
            except Exception as exc:
                resultat = {
                    "modele": ident,
                    "tests": {},
                    "t_moy": args.timeout,
                    "q_moy": 0.0,
                    "indispo": True,
                    "cause": ("errat interne: %s" % exc)[:80],
                }
            details = resultat["tests"]
            t1 = details.get("bug", {}).get("temps_s", 0.0)
            q1 = details.get("bug", {}).get("note", 0.0)
            t2 = details.get("json", {}).get("temps_s", 0.0)
            q2 = details.get("json", {}).get("note", 0.0)
            etat = "INDISPONIBLE" if resultat["indispo"] else "ok"
            print(
                "  %-46s t1=%6.1fs t2=%6.1fs q1=%4.1f q2=%4.1f  %s"
                % (tronquer(nom_court(ident), 46), t1, t2, q1, q2, etat),
                flush=True,
            )
            resultats.append(resultat)

    classes, indisponibles = classer(resultats)
    afficher(classes, indisponibles)

    charge = {
        "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "parametres": {"timeout_s": args.timeout, "workers": args.workers, "limit": args.limit},
        "classement": [
            {
                "rang": rang,
                "modele": resultat["modele"],
                "affichage": nom_court(resultat["modele"]),
                "temps_moyen_s": round(resultat["t_moy"], 3),
                "qualite_moyenne": round(resultat["q_moy"], 2),
                "note_vitesse": round(resultat["note_vitesse"], 2),
                "note_qualite": round(resultat["note_qualite"], 2),
                "note_finale": resultat["note_finale"],
                "tests": resultat["tests"],
            }
            for rang, resultat in enumerate(classes, 1)
        ],
        "indisponibles": [
            {
                "modele": resultat["modele"],
                "cause": resultat["cause"][:80],
                "tests": resultat.get("tests", {}),
            }
            for resultat in indisponibles
        ],
    }

    chemin = Path(args.out)
    chemin.write_text(json.dumps(charge, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nRapport JSON : %s" % chemin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
