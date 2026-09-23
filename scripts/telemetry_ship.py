#!/usr/bin/env python3
"""Expédie la télémétrie de développement vers la branche git `telemetry`.

Transport retenu (décision utilisateur) : git seulement. Pas d'endpoint HTTP
d'ingestion — cela supposerait d'ouvrir un port en écriture sur le VPS, de
provisionner un secret dans chaque session et d'élargir la politique réseau.
Le prix : la latence est celle d'une expédition, pas de la milliseconde.

La branche est une BOÎTE AUX LETTRES, pas un historique : chaque expédition
écrit un commit SANS PARENT, poussé en force. Sans cela, un fichier de
plusieurs centaines de kilo-octets expédié plusieurs fois par jour ferait
enfler le dépôt indéfiniment.

Rien ne passe par `git add` : les deux fichiers sont dans `.gitignore` et ne
doivent jamais entrer dans l'historique normal. On écrit directement les objets
(`hash-object`, `mktree`, `commit-tree`), ce qui ne touche ni l'index, ni
l'arbre de travail, ni la branche courante.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_BRANCH = "telemetry"
DEFAULT_MAX_LINES = 2000
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ce que l'on expédie, et sous quel nom dans l'arbre.
FILES = {
    "telemetry.jsonl": os.path.join(".claude", "telemetry.jsonl"),
    "gemini-receipts.jsonl": os.path.join(".claude", "gemini-receipts.jsonl"),
}


def run(args, repo_dir, input_text=None):
    r = subprocess.run(["git", "-C", repo_dir] + args, input=input_text,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} : {r.stderr.strip()}")
    return r.stdout.strip()


def tail_lines(path, n):
    """`n` dernières lignes, ou chaîne vide si le fichier est absent.

    Lecture par blocs depuis la fin : ces journaux montent à plusieurs
    mégaoctets et l'expédition tourne à chaque fin de tour."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size == 0:
        return ""
    chunks, remaining, newlines = [], size, 0
    with open(path, "rb") as f:
        while remaining > 0 and newlines <= n:
            step = min(65536, remaining)
            remaining -= step
            f.seek(remaining)
            block = f.read(step)
            newlines += block.count(b"\n")
            chunks.insert(0, block)
    data = b"".join(chunks).decode("utf-8", errors="replace")
    lines = [ln for ln in data.split("\n") if ln.strip()]
    # Le premier bloc lu peut commencer au milieu d'une ligne : on n'en garde
    # que `n`, ce qui l'élimine dès que le fichier dépasse un bloc.
    return "\n".join(lines[-n:]) + "\n"


def ship(repo_dir=_REPO, files=None, branch=DEFAULT_BRANCH,
         max_lines=DEFAULT_MAX_LINES, push=True):
    files = files or {name: os.path.join(repo_dir, rel) for name, rel in FILES.items()}
    entries, tree_lines = {}, []
    for name, path in sorted(files.items()):
        content = tail_lines(path, max_lines)
        if not content.strip():
            continue
        sha = run(["hash-object", "-w", "--stdin"], repo_dir, input_text=content)
        entries[name] = content.count("\n")
        tree_lines.append(f"100644 blob {sha}\t{name}")

    if not tree_lines:
        return {"commit": None, "entries": {}, "pushed": False}

    tree = run(["mktree"], repo_dir, input_text="\n".join(tree_lines) + "\n")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    msg = ("Télémétrie de développement — " + stamp + "\n\n"
           + "\n".join(f"{n} : {c} ligne(s)" for n, c in sorted(entries.items())))
    commit = run(["commit-tree", tree, "-m", msg], repo_dir)
    run(["update-ref", f"refs/heads/{branch}", commit], repo_dir)

    pushed = False
    if push:
        run(["push", "--force", "origin", f"{branch}:{branch}"], repo_dir)
        pushed = True
    return {"commit": commit, "entries": entries, "pushed": pushed}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=_REPO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument("--no-push", action="store_true",
                    help="crée le commit local sans le pousser (mise au point)")
    a = ap.parse_args()
    try:
        res = ship(a.repo, branch=a.branch, max_lines=a.max_lines, push=not a.no_push)
    except Exception as e:
        # Expédier est un effet de bord : échouer ne doit pas casser un tour de
        # travail, mais l'erreur doit rester visible.
        sys.stderr.write(f"[telemetry_ship] échec : {e}\n")
        return 1
    if res["commit"] is None:
        print("[telemetry_ship] rien à expédier")
        return 0
    lignes = ", ".join(f"{n}={c}" for n, c in sorted(res["entries"].items()))
    print(f"[telemetry_ship] {res['commit'][:10]} ({lignes})"
          + (" poussé" if res["pushed"] else " local"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
