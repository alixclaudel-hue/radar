#!/usr/bin/env python3
"""Réceptionne la télémétrie de développement depuis la branche git `telemetry`.

Pendant de `telemetry_ship.py`, à lancer sur le VPS. Les sessions de
développement qui ne tournent PAS sur le VPS expédient leurs journaux sur une
branche git ; ce script les verse dans le dossier que lit `radar_ops`, lequel
ne connaît rien à git (il ne voit que le volume `/data`).

La branche est une boîte aux lettres écrasée à chaque expédition par un commit
sans parent : le rapatriement force donc une référence dédiée
(`refs/remotes/origin/<branche>`) au lieu d'un `pull`, et ne touche ni l'index
ni la branche courante du checkout de production.

La fusion est incrémentale et se fait sur `ts` : seules les lignes plus
récentes que la dernière déjà stockée sont ajoutées. Les expéditions se
recouvrent largement (chacune renvoie les 2000 dernières lignes), et sans ce
filtre le fichier local se peuplerait de doublons.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

DEFAULT_BRANCH = "telemetry"
DEFAULT_FILES = ("telemetry.jsonl", "gemini-receipts.jsonl")
MAX_BYTES = 5 * 1024 * 1024
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Même défaut que `radar_web.radar.paths`, sans l'importer : ce script tourne
# sur l'hôte, hors du conteneur, et ne doit pas dépendre de l'application.
_DATA = os.environ.get("CRATE_DATA_DIR") or os.path.join(_REPO, "data")
# Sous-dossier `remote/` et NON le dossier que le hook alimente : sur le VPS,
# une session locale écrit dans `<ops>/telemetry.jsonl` à l'instant présent. Si
# la réception visait ce même fichier, son `ts` maximal serait toujours plus
# récent que les lignes expédiées, qui seraient toutes rejetées en silence.
DEFAULT_DEST = os.path.join(_DATA, "ops", "remote")


def run(args, repo_dir, input_text=None):
    r = subprocess.run(["git", "-C", repo_dir] + args, input=input_text,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} : {r.stderr.strip()}")
    return r.stdout.strip()


def read_branch_file(repo_dir, ref, name):
    """Contenu du fichier à la pointe de la référence, ou chaîne vide.

    Un fichier absent de la boîte aux lettres n'est pas une erreur : une
    session peut n'avoir expédié que des reçus Gemini, sans télémétrie."""
    r = subprocess.run(["git", "-C", repo_dir, "show", f"{ref}:{name}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _ts(line):
    """Horodatage d'une ligne JSONL, ou None si elle n'en porte pas d'exploitable."""
    try:
        row = json.loads(line)
        return float(row["ts"])
    except (ValueError, TypeError, KeyError):
        return None


def max_ts(path):
    """Plus grand `ts` déjà stocké localement, ou None sur fichier absent ou vide."""
    latest = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = _ts(line.strip())
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
    except OSError:
        return None
    return latest


def _rotate_if_needed(path, max_b=MAX_BYTES):
    """Bascule le fichier en `.1` au-delà de `max_b`, une seule génération gardée.

    Même politique que `opslog` et que le hook de capture : le VPS a déjà saturé
    son disque deux fois sur des traces non bornées (CLAUDE.md pt 12), et ce
    fichier-ci grossit à chaque réception sans que rien ne l'élague. Après
    rotation, le `ts` plancher retombe à None et la réception suivante réécrit
    la fenêtre entière depuis la branche — c'est voulu, pas une perte."""
    try:
        if os.path.getsize(path) > max_b:
            os.replace(path, path + ".1")
    except OSError:
        pass


def merge(local_path, incoming_text):
    """Ajoute les lignes postérieures au dernier `ts` local. Renvoie leur nombre.

    Réécriture complète par fichier temporaire puis `os.replace` : `radar_ops`
    lit ce fichier pendant qu'on y écrit, et un `open(..., 'a')` interrompu lui
    montrerait une ligne coupée en deux."""
    if not incoming_text.strip():
        return 0

    floor = max_ts(local_path)
    fresh = []
    for line in incoming_text.splitlines():
        line = line.strip()
        ts = _ts(line) if line else None
        if ts is not None and (floor is None or ts > floor):
            fresh.append((ts, line))
    if not fresh:
        return 0
    fresh.sort(key=lambda r: r[0])

    dest_dir = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(dest_dir, exist_ok=True)
    _rotate_if_needed(local_path)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".telemetry_pull_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            try:
                with open(local_path, "r", encoding="utf-8", errors="replace") as old:
                    kept = old.read()
            except OSError:
                kept = ""
            if kept and not kept.endswith("\n"):
                kept += "\n"
            out.write(kept)
            out.write("\n".join(line for _, line in fresh) + "\n")
        os.replace(tmp, local_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(fresh)


def pull(repo_dir=_REPO, dest_dir=DEFAULT_DEST, branch=DEFAULT_BRANCH,
         names=DEFAULT_FILES, fetch=True):
    ref = f"refs/remotes/origin/{branch}"
    if fetch:
        run(["fetch", "origin", f"+refs/heads/{branch}:{ref}"], repo_dir)
    return {name: merge(os.path.join(dest_dir, name),
                        read_branch_file(repo_dir, ref, name))
            for name in sorted(names)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=_REPO, help="racine du dépôt git")
    ap.add_argument("--dest", default=DEFAULT_DEST,
                    help="dossier lu par radar_ops")
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--no-fetch", action="store_true",
                    help="rejoue la fusion sans contacter origin")
    a = ap.parse_args()
    try:
        res = pull(a.repo, a.dest, a.branch, fetch=not a.no_fetch)
    except Exception as e:
        sys.stderr.write(f"[telemetry_pull] échec : {e}\n")
        return 1
    detail = ", ".join(f"{n}={c}" for n, c in sorted(res.items()))
    print(f"[telemetry_pull] {sum(res.values())} ligne(s) ajoutée(s) "
          f"dans {a.dest} ({detail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
