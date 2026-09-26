#!/usr/bin/env python3
"""Ouvrier autonome — boucle d'outils bornée sur un worktree git dédié (plan §5).

Ce script est le **composant B** de la délégation : là où
[`ai_broker.py`](ai_broker.py:1) sert un appel isolé (lire, résumer, premier jet
de code), l'ouvrier exécute un **chantier entier** — écrire une page, durcir un
job, couvrir un module de tests — puis rend un **diff à relire**.

Trois invariants portent sa sûreté :

- **Périmètre.** Tout se passe dans un `git worktree` dédié
  (`<racine>/.worktrees/worker-<tache>`, branche `worker/<tache>`). Le dépôt en
  place n'est jamais touché : Claude relit avec un `git diff` ordinaire, et
  supprime ou adopte à la main. Aucune écriture hors du worktree, jamais dans
  `.git`, jamais dans `~/radar` ni `/data`.
- **Liste blanche.** Le modèle ne peut lancer que `python3 -m py_compile`,
  `python3 -m unittest`, `git status`, `git diff` — sans shell, sans
  métacaractère, sans argument qui sorte du worktree. Aucun `git commit`, aucun
  `git push`, aucun accès réseau.
- **Bornes dures.** Nombre d'étapes, durée, volume de jetons estimé, nombre de
  fichiers modifiables. Atteindre une borne **n'est pas un échec silencieux** :
  c'est un arrêt immédiat, avec rapport et diff partiel. Le chantier est
  interruptible et son état est toujours lisible.

Le contrat « aucun correctif gardé sans mesure » du skill `script-loop`
s'applique : l'ouvrier ne merge rien, il rend un diff.

**Escalade payante.** Si la boucle gratuite n'aboutit pas, elle peut être
relancée sur DeepSeek — mais seulement sur décision explicite
(`--escalate-paid`), jamais automatique en début de tâche, et la décision est
tracée dans le reçu (`escalated`, `paid`, `justified`).

Lancer : `python3 scripts/ai_worker.py "<chantier>" [options]`.
Codes de retour : `0` chantier terminé, `1` erreur (worktree, décision
illisible), `2` arrêt sur borne (rapport quand même rendu).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass

# Lancé en direct (`python3 scripts/ai_worker.py`), `sys.path[0]` vaut `scripts/`
# et le socle `scripts.ai` serait introuvable. Même amorçage que `ai_broker.py`.
_RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RACINE_DEPOT not in sys.path:
    sys.path.insert(0, _RACINE_DEPOT)

from scripts import ai_query  # noqa: E402
from scripts.ai import jsonout  # noqa: E402

# ---------------------------------------------------------------------------
# Constantes de périmètre
# ---------------------------------------------------------------------------

SOUS_DOSSIER_WORKTREES = ".worktrees"
PREFIXE_BRANCHE = "worker/"

#: Commandes que le modèle peut demander, par préfixe de jetons. Rien d'autre ne
#: passe : ni shell, ni `git add`, ni `git commit`, ni `pip`, ni `curl`.
COMMANDES_AUTORISEES: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "py_compile"),
    ("python3", "-m", "unittest"),
    ("git", "status"),
    ("git", "diff"),
)

#: Motifs shell refusés explicitement. On n'utilise pas de shell (argv en liste),
#: donc ils seraient inertes — mais un refus nommé vaut mieux qu'une confiance
#: tacite : le jour où quelqu'un passe à `shell=True`, la garde tient déjà.
MOTIFS_SHELL = (";", "&&", "||", "|", ">", "<", "`", "$(", "${", "\n")

#: Racines hors périmètre : les deux checkouts de production. Comparaison sur les
#: composants de chemin, pour que `~/radar-work` ne soit **pas** confondu avec
#: `~/radar` (l'un commence par l'autre comme chaîne).
PREFIXES_HORS_PERIMETRE = ("/data", os.path.expanduser("~/radar"))

#: Outils exposés au modèle.
OUTILS = ("lire", "lister", "chercher", "ecrire", "patch", "commande", "terminer")

#: Estimation de jetons hors ligne (pas une mesure fournisseur : le journal
#: d'usage `ai_usage.jsonl` reste la seule comptabilité qui fait foi). Elle sert
#: à *borner* la boucle, pas à facturer, et c'est dit dans le rapport.
JETONS_PAR_CARACTERE = 0.25

#: Taille maximale d'un contenu relu, pour qu'un fichier de log ne fasse pas
#: exploser le prompt de l'étape suivante.
LECTURE_MAX = 8000
RESULTAT_MAX = 4000


# ---------------------------------------------------------------------------
# Périmètre : chemins et commandes
# ---------------------------------------------------------------------------

def hors_perimetre(chemin: str) -> bool:
    """Vrai si `chemin` tombe dans une racine interdite (`/data`, `~/radar`)."""
    try:
        reel = os.path.realpath(chemin)
    except OSError:
        return True
    for prefixe in PREFIXES_HORS_PERIMETRE:
        p = os.path.realpath(prefixe)
        if reel == p or reel.startswith(p + os.sep):
            return True
    return False


def resoudre_dans_perimetre(racine: str, chemin: str) -> str:
    """Chemin absolu confiné au worktree, ou lève `ValueError`.

    Refuse : chemin vide, chemin absolu, `~`, toute remontée `..` qui sort du
    worktree, tout composant `.git` (les hooks d'un `.git` sont du code exécuté
    à la prochaine commande git — c'est une évasion de périmètre déguisée), et
    les racines de production.
    """
    if not chemin or not isinstance(chemin, str):
        raise ValueError("chemin vide")
    if os.path.isabs(chemin) or chemin.startswith("~"):
        raise ValueError(f"chemin absolu refusé : {chemin}")
    racine_reelle = os.path.realpath(racine)
    cible = os.path.realpath(os.path.join(racine_reelle, chemin))
    if cible != racine_reelle and not cible.startswith(racine_reelle + os.sep):
        raise ValueError(f"chemin hors du worktree : {chemin}")
    relatif = os.path.relpath(cible, racine_reelle)
    if ".git" in relatif.split(os.sep):
        raise ValueError(f"accès à .git refusé : {chemin}")
    if hors_perimetre(cible):
        raise ValueError(f"chemin hors périmètre : {chemin}")
    return cible


def analyser_commande(commande: str) -> list[str]:
    """Découpe et valide une commande contre la liste blanche, ou lève."""
    if not commande or not isinstance(commande, str):
        raise ValueError("commande vide")
    for motif in MOTIFS_SHELL:
        if motif in commande:
            raise ValueError(f"métacaractère shell refusé : {motif!r}")
    try:
        argv = shlex.split(commande)
    except ValueError as exc:
        raise ValueError(f"commande illisible : {exc}") from exc
    if not argv:
        raise ValueError("commande vide")
    autorisee = any(tuple(argv[: len(pre)]) == pre for pre in COMMANDES_AUTORISEES)
    if not autorisee:
        raise ValueError(f"commande hors liste blanche : {argv[0]!r}")
    return argv


def _verifier_jetons_argv(argv: list[str]) -> None:
    """Aucun argument de commande ne doit sortir du worktree."""
    for jeton in argv[1:]:
        if jeton.startswith("-"):
            continue
        if os.path.isabs(jeton) or jeton.startswith("~"):
            raise ValueError(f"argument absolu refusé : {jeton!r}")
        if ".." in jeton.split("/"):
            raise ValueError(f"argument remontant refusé : {jeton!r}")


# ---------------------------------------------------------------------------
# Git : worktree, diff
# ---------------------------------------------------------------------------

def _git(racine: str, *argv: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv],
        cwd=racine,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def est_depot_git(racine: str) -> bool:
    try:
        p = _git(racine, "rev-parse", "--git-dir")
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def slugifier(tache: str) -> str:
    """Nom de dossier/branche sûr, dérivé de la tâche."""
    base = re.sub(r"[^a-z0-9]+", "-", (tache or "").lower()).strip("-")
    return (base or "chantier")[:40]


def chemin_worktree(racine: str, tache: str) -> str:
    return os.path.join(racine, SOUS_DOSSIER_WORKTREES, "worker-" + slugifier(tache))


def nom_branche(tache: str) -> str:
    return PREFIXE_BRANCHE + slugifier(tache)


def _exclure_du_depot_hote(racine: str, dossier_scratch: str) -> None:
    """Inscrit le dossier des worktrees dans `.git/info/exclude` du dépôt hôte.

    Le worktree vit sous `<racine>/.worktrees/` : sans cette ligne, `git status`
    du dépôt hôte afficherait un dossier non suivi — et un `git add -A` de Claude
    pourrait embarquer le contenu d'un chantier en cours. Un `.gitignore` serait
    un fichier versionné de plus ; `.git/info/exclude` est local, jamais commité,
    et c'est donc la seule écriture faite chez le dépôt hôte. Ne lève jamais.
    """
    relatif = os.path.relpath(os.path.abspath(dossier_scratch), os.path.abspath(racine))
    if relatif in ("", ".") or relatif.startswith(".."):
        return
    motif = relatif.replace(os.sep, "/").rstrip("/") + "/"
    try:
        p = _git(racine, "rev-parse", "--git-dir")
        if p.returncode != 0:
            return
        git_dir = p.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(racine, git_dir)
        chemin_exclude = os.path.join(git_dir, "info", "exclude")
        try:
            with open(chemin_exclude, "r", encoding="utf-8", errors="replace") as f:
                existant = f.read()
        except OSError:
            existant = ""
        if motif in existant.splitlines():
            return
        os.makedirs(os.path.dirname(chemin_exclude), exist_ok=True)
        with open(chemin_exclude, "a", encoding="utf-8") as f:
            if existant and not existant.endswith("\n"):
                f.write("\n")
            f.write(f"# ai_worker — worktrees de chantier (local, jamais commité)\n{motif}\n")
    except (OSError, subprocess.SubprocessError):
        return


def creer_worktree(racine: str, chemin: str, branche: str) -> tuple[bool, str]:
    """Crée (ou réutilise) le worktree dédié. Ne lève jamais.

    Le worktree reste invisible au `git status` du dépôt hôte, grâce à
    `_exclure_du_depot_hote()`. Le dépôt hôte n'est donc jamais perturbé : ni
    fichier suivi, ni dossier non suivi, ni commit.
    """
    _exclure_du_depot_hote(racine, os.path.dirname(os.path.abspath(chemin)))
    marqueur = os.path.join(chemin, ".git")
    if os.path.exists(marqueur):
        return True, "worktree déjà présent"
    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
    except OSError as exc:
        return False, f"dossier impossible à créer : {exc}"
    p = _git(racine, "worktree", "add", "-b", branche, chemin, "HEAD")
    if p.returncode == 0:
        return True, "worktree créé"
    # La branche peut déjà exister (reprise d'un chantier) : on la rattache.
    p2 = _git(racine, "worktree", "add", chemin, branche)
    if p2.returncode == 0:
        return True, "worktree rattaché à une branche existante"
    detail = (p2.stderr or p.stderr or p.stdout or "").strip()
    return False, detail[:400]


def supprimer_worktree(racine: str, chemin: str) -> bool:
    try:
        return _git(racine, "worktree", "remove", "--force", chemin).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def calculer_diff(chemin_worktree_: str) -> dict:
    """Diff unifié du worktree, nouveaux fichiers compris.

    `--intent-to-add` est de la **tenue de maison de l'ouvrier**, pas une
    capacité du modèle : sans lui, un fichier créé (jamais `git add`) resterait
    invisible à `git diff` et le diff rendu à Claude serait incomplet. Rien n'est
    commité : l'index est le seul à bouger, dans le worktree.
    """
    diff = ""
    statut = ""
    try:
        _git(chemin_worktree_, "add", "-A", "--intent-to-add")
        diff = _git(chemin_worktree_, "diff").stdout
        statut = _git(chemin_worktree_, "status", "--porcelain").stdout
    except (OSError, subprocess.SubprocessError) as exc:
        statut = f"(git indisponible : {exc})"
    return {"diff": diff, "statut": statut}


# ---------------------------------------------------------------------------
# Outils exposés
# ---------------------------------------------------------------------------

def outil_lire(racine: str, args: dict) -> str:
    chemin = resoudre_dans_perimetre(racine, args.get("chemin", ""))
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        contenu = f.read()
    if len(contenu) > LECTURE_MAX:
        return contenu[:LECTURE_MAX] + f"\n[...tronqué : {len(contenu)} caractères]"
    return contenu


def outil_lister(racine: str, args: dict) -> str:
    chemin = resoudre_dans_perimetre(racine, args.get("chemin", "."))
    if os.path.isfile(chemin):
        return os.path.relpath(chemin, racine)
    try:
        noms = sorted(os.listdir(chemin))[:200]
    except OSError as exc:
        raise ValueError(f"listing impossible : {exc}") from exc
    lignes = []
    for nom in noms:
        if nom == ".git":
            continue
        complet = os.path.join(chemin, nom)
        lignes.append(nom + "/" if os.path.isdir(complet) else nom)
    return "\n".join(lignes) if lignes else "(vide)"


def outil_chercher(racine: str, args: dict) -> str:
    motif = args.get("motif", "")
    if not motif:
        raise ValueError("motif vide")
    try:
        rx = re.compile(motif)
    except re.error as exc:
        raise ValueError(f"motif invalide : {exc}") from exc
    resultats: list[str] = []
    parcourus = 0
    for base, dossiers, fichiers in os.walk(racine):
        dossiers[:] = [d for d in dossiers if d != ".git"]
        for nom in fichiers:
            parcourus += 1
            chemin = os.path.join(base, nom)
            try:
                with open(chemin, "r", encoding="utf-8", errors="replace") as f:
                    for numero, ligne in enumerate(f, 1):
                        if rx.search(ligne):
                            rel = os.path.relpath(chemin, racine)
                            resultats.append(f"{rel}:{numero}: {ligne.strip()[:160]}")
                            break
            except OSError:
                continue
            if len(resultats) >= 60:
                break
        if len(resultats) >= 60:
            break
    if not resultats:
        return f"aucune correspondance ({parcourus} fichier(s) parcouru(s))"
    return "\n".join(resultats)


def outil_ecrire(racine: str, args: dict) -> str:
    chemin = resoudre_dans_perimetre(racine, args.get("chemin", ""))
    contenu = args.get("contenu", "")
    if not isinstance(contenu, str):
        raise ValueError("'contenu' doit être une chaîne")
    parent = os.path.dirname(chemin) or racine
    os.makedirs(parent, exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)
    return f"écrit {len(contenu)} caractère(s) dans {os.path.relpath(chemin, racine)}"


def outil_patch(racine: str, args: dict) -> str:
    chemin = resoudre_dans_perimetre(racine, args.get("chemin", ""))
    ancien = args.get("ancien", "")
    nouveau = args.get("nouveau", "")
    if not ancien:
        raise ValueError("'ancien' vide")
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        contenu = f.read()
    occurrences = contenu.count(ancien)
    if occurrences != 1:
        raise ValueError(f"'ancien' présent {occurrences} fois (exactement 1 attendu)")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu.replace(ancien, nouveau, 1))
    return f"patch appliqué à {os.path.relpath(chemin, racine)}"


def executer_commande(racine: str, args: dict, *, timeout: int = 120) -> dict:
    """Lance une commande de la liste blanche dans le worktree."""
    argv = analyser_commande(args.get("commande", ""))
    _verifier_jetons_argv(argv)
    proc = subprocess.run(
        argv, cwd=racine, capture_output=True, text=True, check=False, timeout=timeout
    )
    return {
        "commande": args.get("commande", ""),
        "argv": argv,
        "code": proc.returncode,
        "stdout": proc.stdout[-RESULTAT_MAX:],
        "stderr": proc.stderr[-RESULTAT_MAX:],
    }


def executer_outil(racine: str, outil: str, args: dict) -> str | dict:
    """Répartit un outil. Lève `ValueError` sur refus de périmètre."""
    if outil == "lire":
        return outil_lire(racine, args)
    if outil == "lister":
        return outil_lister(racine, args)
    if outil == "chercher":
        return outil_chercher(racine, args)
    if outil == "ecrire":
        return outil_ecrire(racine, args)
    if outil == "patch":
        return outil_patch(racine, args)
    if outil == "commande":
        return executer_commande(racine, args)
    raise ValueError(f"outil inconnu : {outil!r}")


# ---------------------------------------------------------------------------
# Appel au modèle
# ---------------------------------------------------------------------------

def _decider_courtier(
    prompt: str,
    *,
    allow_paid: bool = False,
    provider: str | None = None,
    modele: str | None = None,
    mode: str = "general",
    effort: str | None = None,
) -> tuple[str, dict]:
    """Décision par défaut : un appel `ai_broker` en sous-processus.

    En sous-processus, et non par import : le courtier écrit son reçu, son
    journal d'usage et son éventuel cooldown dans des fichiers globaux ; l'isoler
    garantit qu'un échec de l'ouvrier ne laisse pas d'état partagé à moitié écrit
    dans le processus appelant. `--json-output` est passé pour que la réponse soit
    du JSON validé par la passerelle, pas une prose à re-découper ici.
    """
    argv = [
        sys.executable,
        os.path.join(_RACINE_DEPOT, "scripts", "ai_broker.py"),
        prompt,
        "--mode",
        mode,
        "--json-output",
    ]
    if allow_paid:
        argv.append("--allow-paid")
    if provider:
        argv += ["--provider", provider]
    if modele:
        argv += ["-m", modele]
    if effort:
        argv += ["--effort", effort]
    proc = subprocess.run(
        argv, cwd=_RACINE_DEPOT, capture_output=True, text=True, check=False, timeout=900
    )
    texte = proc.stdout
    meta = {
        "code": proc.returncode,
        "provider": provider,
        "modele": modele,
        "paid": bool(allow_paid),
        "tokens_estimes": int((len(prompt) + len(texte)) * JETONS_PAR_CARACTERE),
    }
    if proc.returncode != 0 and not texte.strip():
        raise RuntimeError(f"courtier en échec (code {proc.returncode})")
    return texte, meta


def _decider_paye(prompt: str) -> tuple[str, dict]:
    """Escalade : DeepSeek, mode `reasoning`, payant explicitement autorisé."""
    return _decider_courtier(
        prompt, allow_paid=True, provider="deepseek", mode="reasoning", effort="high"
    )


# ---------------------------------------------------------------------------
# Bornes
# ---------------------------------------------------------------------------

@dataclass
class Bornes:
    """Bornes dures de la boucle. Les atteindre arrête proprement, avec rapport."""

    etapes: int = 12
    secondes: float = 900.0
    jetons: int = 60000
    fichiers: int = 6

    def as_dict(self) -> dict:
        return {
            "etapes": self.etapes,
            "secondes": self.secondes,
            "jetons": self.jetons,
            "fichiers": self.fichiers,
        }


# ---------------------------------------------------------------------------
# Prompt de la boucle
# ---------------------------------------------------------------------------

CONTRAT_OUTILS = """Tu es un ouvrier autonome. Tu ne discutes pas : tu agis.
Tu travailles EXCLUSIVEMENT dans ce worktree : {worktree}
Tu réponds par UN SEUL objet JSON, sans texte ni balise autour :
{{"pensee": "en une phrase", "outil": "<outil>", "arguments": {{...}}}}

Outils disponibles :
- lire      {{"chemin": "relatif"}}
- lister    {{"chemin": "relatif ou ."}}
- chercher  {{"motif": "expression régulière"}}
- ecrire    {{"chemin": "relatif", "contenu": "texte complet du fichier"}}
- patch     {{"chemin": "relatif", "ancien": "texte exact unique", "nouveau": "..."}}
- commande  {{"commande": "python3 -m py_compile x.py"}}
  liste blanche : python3 -m py_compile, python3 -m unittest, git status, git diff
- terminer  {{"resume": "ce qui est fait, ce qui passe, ce qui reste rouge"}}

Interdits, sans exception : chemin absolu, `~`, `..`, `.git`, commande hors
liste blanche, métacaractère shell, réseau, git commit, git push.
Appelle `terminer` dès que le chantier est fait ou bloqué.
"""


def construire_prompt(tache: str, worktree: str, journal: list[str], passe_payante: bool) -> str:
    entete = CONTRAT_OUTILS.format(worktree=worktree)
    if passe_payante:
        entete += (
            "\nC'est une SECONDE PASSE payante : la première, gratuite, n'a pas "
            "abouti. Reprends l'état du worktree tel quel, ne repars pas de zéro.\n"
        )
    corps = "\n\n## Chantier\n" + tache.strip()
    if journal:
        corps += "\n\n## Déroulé des dernières actions\n" + "\n".join(journal[-10:])
    return entete + corps


# ---------------------------------------------------------------------------
# Boucle bornée
# ---------------------------------------------------------------------------

def _journaliser(journal: list[str], entree: str, garder: int = 14) -> None:
    journal.append(entree)
    del journal[:-garder]


def _arreter(rapport: dict, borne: str | None, *, statut: str | None = None) -> dict:
    rapport["statut"] = statut or ("borne" if borne else "erreur")
    rapport["arret"] = borne
    return rapport


def _boucle(
    rapport: dict,
    *,
    worktree: str,
    bornes: Bornes,
    journal: list[str],
    decider,
    horloge,
    t0: float,
    passe_payante: bool,
) -> dict:
    """Une passe de la boucle. Mute `rapport` et le rend."""
    erreurs_illisibles = 0
    while True:
        if rapport["etapes"] >= bornes.etapes:
            return _arreter(rapport, "etapes")
        if horloge() - t0 >= bornes.secondes:
            return _arreter(rapport, "secondes")
        if rapport["jetons_estimes"] >= bornes.jetons:
            return _arreter(rapport, "jetons")

        prompt = construire_prompt(rapport["tache"], worktree, journal, passe_payante)
        try:
            texte, meta = decider(prompt)
        except Exception as exc:  # noqa: BLE001 — toute panne de décision arrête
            rapport["erreurs"].append(f"décision impossible : {exc!r}")
            return _arreter(rapport, "decision", statut="erreur")

        rapport["etapes"] += 1
        if isinstance(meta, dict):
            rapport["jetons_estimes"] += int(meta.get("tokens_estimes") or 0)
            rapport["paye"] = bool(meta.get("paid"))
            rapport["fournisseur"] = meta.get("provider") or rapport["fournisseur"]
            rapport["modele"] = meta.get("modele") or rapport["modele"]

        charge = jsonout.extract_json_object(texte or "")
        if not isinstance(charge, dict) or not charge.get("outil"):
            erreurs_illisibles += 1
            rapport["erreurs"].append("réponse du modèle sans objet JSON exploitable")
            _journaliser(journal, "ERREUR: réponse illisible, je redemande un JSON valide")
            if erreurs_illisibles >= 2:
                return _arreter(rapport, "illisible", statut="erreur")
            continue
        erreurs_illisibles = 0

        outil = str(charge.get("outil"))
        arguments = charge.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        rapport["tentatives"].append(outil)

        if outil == "terminer":
            rapport["resume"] = str(arguments.get("resume") or "")
            return _arreter(rapport, None, statut="termine")
        if outil not in OUTILS:
            _journaliser(journal, f"REFUS: outil inconnu {outil!r}")
            continue

        # Bornes de fichiers : refus AVANT écriture, pour que le worktree reste
        # exactement dans l'enveloppe annoncée.
        if outil in ("ecrire", "patch"):
            try:
                cible = resoudre_dans_perimetre(worktree, arguments.get("chemin", ""))
            except ValueError as exc:
                rapport["refus"].append(str(exc))
                _journaliser(journal, f"REFUS: {exc}")
                continue
            relatif = os.path.relpath(cible, worktree)
            if relatif not in rapport["fichiers_modifies"]:
                if len(rapport["fichiers_modifies"]) >= bornes.fichiers:
                    rapport["refus"].append(f"borne de fichiers atteinte : {relatif}")
                    return _arreter(rapport, "fichiers")
                rapport["fichiers_modifies"].append(relatif)

        try:
            resultat = executer_outil(worktree, outil, arguments)
        except ValueError as exc:
            rapport["refus"].append(str(exc))
            _journaliser(journal, f"REFUS ({outil}): {exc}")
            continue
        except OSError as exc:
            rapport["erreurs"].append(f"{outil} a échoué : {exc}")
            _journaliser(journal, f"ERREUR ({outil}): {exc}")
            continue

        if outil == "commande":
            rapport["commandes"].append(
                {"commande": resultat["commande"], "code": resultat["code"]}
            )
            if resultat["code"] == 0:
                rapport["passent"].append(resultat["commande"])
                resume = f"code 0\n{resultat['stdout']}".strip()
            else:
                rapport["restent_rouges"].append(
                    {"commande": resultat["commande"], "code": resultat["code"]}
                )
                resume = (
                    f"code {resultat['code']}\n{resultat['stdout']}\n{resultat['stderr']}"
                ).strip()
            _journaliser(journal, f"commande {resultat['commande']!r} -> code {resultat['code']}")
            _journaliser(journal, f"    {resume[:400]}")
        else:
            texte_resultat = str(resultat)
            _journaliser(journal, f"{outil} -> {texte_resultat[:400]}")


# ---------------------------------------------------------------------------
# Reçu
# ---------------------------------------------------------------------------

def ecrire_recu_worker(rapport: dict) -> None:
    """Trace le chantier dans le journal de reçus (tableau de bord + gate).

    `mode="worker"` : ce reçu ne satisfait aucune exigence du gate (qui attend
    `code`, `test`…), et c'est voulu — l'ouvrier ne remplace pas la règle n°1,
    il l'exécute en profondeur. Les appels réels du courtier, eux, ont déjà
    laissé leurs propres reçus avec leur mode.
    """
    ai_query.write_receipt(
        "worker",
        rapport.get("statut", "erreur"),
        source="ai_worker",
        provider=rapport.get("fournisseur"),
        model=rapport.get("modele"),
        tier="paid" if rapport.get("paye") else "free",
        paid=rapport.get("paye"),
        justified=rapport.get("justifie"),
        escalated=rapport.get("justifie"),
        steps=rapport.get("etapes"),
        files=len(rapport.get("fichiers_modifies") or []),
        borne=rapport.get("arret"),
        worktree=rapport.get("worktree"),
        branch=rapport.get("branche"),
        session=os.environ.get("CLAUDE_SESSION_ID"),
        prompt_chars=len(rapport.get("tache") or ""),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _rapport_vierge(tache: str, racine: str, bornes: Bornes) -> dict:
    return {
        "tache": tache,
        "racine": racine,
        "statut": "erreur",
        "arret": None,
        "worktree": None,
        "branche": None,
        "bornes": bornes.as_dict(),
        "etapes": 0,
        "jetons_estimes": 0,
        "duree_s": 0.0,
        "paye": False,
        "justifie": False,
        "fournisseur": None,
        "modele": None,
        "tentatives": [],
        "commandes": [],
        "passent": [],
        "restent_rouges": [],
        "fichiers_modifies": [],
        "refus": [],
        "erreurs": [],
        "passes": [],
        "resume": "",
        "diff": "",
        "statut_git": "",
    }


def executer(
    tache: str,
    *,
    racine: str | None = None,
    worktree: str | None = None,
    branche: str | None = None,
    bornes: Bornes | None = None,
    decider=None,
    decider_paye=None,
    escalade_payante: bool = False,
    conserver: bool = True,
    horloge=None,
    inscrire_recu: bool = True,
) -> dict:
    """Exécute un chantier et rend le rapport (dict sérialisable).

    `decider(prompt) -> (texte, meta)` est injectable : c'est le point par lequel
    le banc fait tourner la boucle hors ligne, sans réseau ni clé.
    """
    racine = os.path.abspath(racine or _RACINE_DEPOT)
    bornes = bornes or Bornes()
    horloge = horloge or time.monotonic
    rapport = _rapport_vierge(tache, racine, bornes)

    if not est_depot_git(racine):
        rapport["erreurs"].append(f"{racine} n'est pas un dépôt git")
        if inscrire_recu:
            ecrire_recu_worker(rapport)
        return rapport

    branche = branche or nom_branche(tache)
    chemin_wt = os.path.abspath(worktree or chemin_worktree(racine, tache))
    rapport["branche"] = branche
    rapport["worktree"] = chemin_wt

    ok, message = creer_worktree(racine, chemin_wt, branche)
    if not ok:
        rapport["erreurs"].append(f"worktree impossible : {message}")
        if inscrire_recu:
            ecrire_recu_worker(rapport)
        return rapport

    if decider is None:
        def decider(prompt: str):  # noqa: E306 — closure sur le courtier par défaut
            return _decider_courtier(prompt)

    t0 = horloge()
    journal: list[str] = []
    _boucle(
        rapport,
        worktree=chemin_wt,
        bornes=bornes,
        journal=journal,
        decider=decider,
        horloge=horloge,
        t0=t0,
        passe_payante=False,
    )
    rapport["passes"].append({"paye": False, "etapes": rapport["etapes"], "statut": rapport["statut"]})
    etapes_gratuites = rapport["etapes"]
    # Chaque passe a son propre budget d'étapes ; le rapport rend le total.
    rapport["etapes"] = 0

    # --- Escalade : jamais automatique, jamais au début, toujours tracée ---
    if rapport["statut"] != "termine" and escalade_payante:
        rapport["justifie"] = True
        _boucle(
            rapport,
            worktree=chemin_wt,
            bornes=bornes,
            journal=journal,
            decider=decider_paye or _decider_paye,
            horloge=horloge,
            t0=t0,
            passe_payante=True,
        )
        rapport["paye"] = True
        rapport["passes"].append(
            {"paye": True, "etapes": rapport["etapes"], "statut": rapport["statut"]}
        )
    elif rapport["statut"] != "termine":
        rapport["escalade_disponible"] = True

    rapport["etapes"] += etapes_gratuites
    rapport["duree_s"] = round(max(0.0, horloge() - t0), 3)

    etat = calculer_diff(chemin_wt)
    rapport["diff"] = etat["diff"]
    rapport["statut_git"] = etat["statut"]

    if not conserver:
        rapport["nettoye"] = supprimer_worktree(racine, chemin_wt)

    if inscrire_recu:
        ecrire_recu_worker(rapport)
    return rapport


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Ouvrier autonome : boucle d'outils bornée sur un worktree git dédié."
    )
    parser.add_argument("tache", nargs="?", default="", help="Chantier à mener, en clair.")
    parser.add_argument("--racine", help="Dépôt git de référence (défaut : ce dépôt).")
    parser.add_argument("--worktree", help="Chemin du worktree (défaut : .worktrees/worker-<slug>).")
    parser.add_argument("--branche", help="Branche du worktree (défaut : worker/<slug>).")
    parser.add_argument("--max-steps", type=int, default=Bornes.etapes, dest="max_steps")
    parser.add_argument("--max-seconds", type=float, default=Bornes.secondes, dest="max_seconds")
    parser.add_argument("--max-tokens", type=int, default=Bornes.jetons, dest="max_tokens")
    parser.add_argument("--max-files", type=int, default=Bornes.fichiers, dest="max_files")
    parser.add_argument(
        "--escalate-paid",
        action="store_true",
        dest="escalade_payante",
        help="Autorise UNE relance payante (DeepSeek) si la boucle gratuite n'aboutit pas.",
    )
    parser.add_argument(
        "--nettoyer",
        action="store_true",
        help="Supprime le worktree en fin de course (défaut : le conserver pour relecture).",
    )
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Rapport JSON sur stdout.")
    parser.add_argument("-o", "--output", help="Écrit le rapport JSON dans un fichier.")
    parser.add_argument("--diff", action="store_true", help="Imprime aussi le diff unifié.")
    return parser.parse_args(argv)


def _resume_humain(rapport: dict) -> str:
    lignes = [
        f"Chantier    : {rapport['tache']}",
        f"Statut      : {rapport['statut']}"
        + (f" (arrêt sur borne « {rapport['arret']} »)" if rapport["arret"] else ""),
        f"Worktree    : {rapport['worktree']} [{rapport['branche']}]",
        f"Étapes      : {rapport['etapes']} · jetons estimés : {rapport['jetons_estimes']}"
        f" · durée : {rapport['duree_s']}s",
        f"Fichiers    : {', '.join(rapport['fichiers_modifies']) or '(aucun)'}",
        f"Commandes   : {len(rapport['commandes'])}"
        f" (vertes {len(rapport['passent'])}, rouges {len(rapport['restent_rouges'])})",
    ]
    if rapport["resume"]:
        lignes.append(f"Résumé      : {rapport['resume']}")
    if rapport["refus"]:
        lignes.append(f"Refus       : {len(rapport['refus'])} hors périmètre ou hors liste blanche")
    if rapport["erreurs"]:
        lignes.append(f"Erreurs     : {'; '.join(rapport['erreurs'][:3])}")
    if rapport.get("escalade_disponible"):
        lignes.append("Escalade    : disponible (relancer avec --escalate-paid)")
    if rapport["passes"]:
        lignes.append(
            "Passes      : " + " | ".join(
                f"{'payante' if p['paye'] else 'gratuite'} {p['etapes']} étapes -> {p['statut']}"
                for p in rapport["passes"]
            )
        )
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.tache.strip():
        sys.stderr.write("Erreur : aucun chantier fourni.\n")
        return 1

    bornes = Bornes(
        etapes=args.max_steps,
        secondes=args.max_seconds,
        jetons=args.max_tokens,
        fichiers=args.max_files,
    )
    rapport = executer(
        args.tache,
        racine=args.racine,
        worktree=args.worktree,
        branche=args.branche,
        bornes=bornes,
        escalade_payante=args.escalade_payante,
        conserver=not args.nettoyer,
    )

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(rapport, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            sys.stderr.write(f"Erreur : rapport non écrit ({exc}).\n")
            return 1

    if args.json_mode:
        json.dump(rapport, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(_resume_humain(rapport))

    if args.diff and rapport["diff"]:
        print(rapport["diff"])

    if rapport["statut"] == "termine":
        return 0
    if rapport["statut"] == "borne":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
