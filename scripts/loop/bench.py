#!/usr/bin/env python3
"""Banc générique de la boucle autonome — mesure un script, compare deux mesures.

Ne connaît AUCUN script en particulier : il lit `scripts/loop/registry.json`,
lance la commande de banc qui y est déclarée avec `--json`, et normalise.

Le contrat qu'un banc doit respecter pour entrer dans la boucle tient en une
ligne : accepter `--json` et écrire sur stdout

    {"script": "<nom>", "adversarial": {"ok": N, "total": M},
                        "floor": {"ok": N, "total": M}, "failed": ["id", ...]}

Les deux familles sont séparées parce qu'elles ne disent pas la même chose :

- `adversarial` = cas construits depuis de VRAIS échecs de production. C'est la
  seule famille qui mesure une amélioration.
- `floor` = cas rejouant des succès connus. Ne détecte qu'une casse franche —
  mesuré le 19/09 sur ytcache : le plancher passait encore à 25/25 avec le
  scoring entièrement court-circuité. Ne jamais lire une progression dedans.

Usage :
    python3 scripts/loop/bench.py ytcache                  # mesure, affiche
    python3 scripts/loop/bench.py ytcache --save avant.json
    python3 scripts/loop/bench.py ytcache --compare avant.json

`--compare` est ce qui décide si un correctif est gardé. Sa sortie est un
verdict explicite (AMÉLIORATION / NEUTRE / RÉGRESSION) et son code de retour
vaut 0 seulement pour les deux premiers.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
REGISTRY = os.path.join(HERE, "registry.json")


def load_registry():
    with open(REGISTRY, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def measure(script):
    reg = load_registry()
    if script not in reg:
        sys.exit(f"Script inconnu : {script!r}. Connus : {', '.join(sorted(reg))}.\n"
                 f"Pour en ajouter un, il lui faut d'abord un banc et une source "
                 f"d'observations — cf. l'en-tête de {REGISTRY}.")
    cmd = reg[script]["bench"] + ["--json"]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if not p.stdout.strip():
        sys.exit(f"Le banc de {script!r} n'a rien écrit sur stdout.\n"
                 f"commande : {' '.join(cmd)}\nstderr : {p.stderr[:2000]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        sys.exit(f"Le banc de {script!r} n'a pas produit de JSON valide "
                 f"(contrat --json non respecté).\nstdout : {p.stdout[:2000]}")


def _fmt(m):
    a, f = m["adversarial"], m["floor"]
    return (f"adverses {a['ok']}/{a['total']} (discriminants) · "
            f"plancher {f['ok']}/{f['total']} (non discriminant)")


def compare(before, after):
    """Verdict + code de retour. La règle est volontairement asymétrique : une
    progression ne compte que sur les cas adverses, alors qu'une régression
    compte sur les DEUX familles (le plancher ne prouve rien quand il monte,
    mais quand il casse, quelque chose est franchement cassé)."""
    da = after["adversarial"]["ok"] - before["adversarial"]["ok"]
    df = after["floor"]["ok"] - before["floor"]["ok"]
    print(f"avant : {_fmt(before)}")
    print(f"après : {_fmt(after)}")
    print(f"delta : adverses {da:+d} · plancher {df:+d}")
    if after["failed"]:
        print(f"échecs restants : {', '.join(after['failed'])}")

    if da < 0 or df < 0:
        print("\nVERDICT : RÉGRESSION — ne pas garder ce correctif.")
        return 1
    if da > 0:
        print(f"\nVERDICT : AMÉLIORATION — {da} cas adverse(s) de plus, aucune régression.")
        return 0
    print("\nVERDICT : NEUTRE — rien ne régresse, mais rien ne progresse non plus.\n"
          "Un correctif neutre ne se merge pas tout seul : soit le cas adverse qui\n"
          "aurait dû bouger manque au corpus, soit le correctif ne traite pas la\n"
          "cause réelle.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="nom dans scripts/loop/registry.json")
    ap.add_argument("--save", metavar="FICHIER", help="écrit la mesure pour un --compare ultérieur")
    ap.add_argument("--compare", metavar="FICHIER", help="compare la mesure courante à celle-ci")
    args = ap.parse_args()

    now = measure(args.script)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(now, f, ensure_ascii=False, indent=2)
        print(f"mesure enregistrée dans {args.save}")
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            return compare(json.load(f), now)
    print(_fmt(now))
    if now["adversarial"]["total"] == 0:
        print("\nATTENTION : aucun cas adverse pour ce script. La boucle ne peut rien\n"
              "mesurer — il faut d'abord collecter de vrais échecs de production\n"
              "(skill script-observe côté VPS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
