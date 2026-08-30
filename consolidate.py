"""
consolidate.py — fusionne les labels du corpus de goût dans la base de labels de
Crate Radar, avec un nettoyage des faux labels (éditeurs, distributeurs, placeholders).

Lance :
    python3 consolidate.py            # applique
    python3 consolidate.py --dry      # montre ce qui serait fait, sans écrire

⚠️ Ferme l'appli Streamlit avant de lancer (elle réécrirait la config au prochain persist).

Modifie :
  - crate_radar_config.json   -> clé "labels" (ajout des nouveaux)
  - labels_resolved.json      -> marque les labels du corpus comme "confirmed" (source corpus)
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "crate_radar_config.json")
CORPUS_PATH = os.path.join(HERE, "taste_corpus.json")
RESOLVED_PATH = os.path.join(HERE, "labels_resolved.json")

DRY = "--dry" in sys.argv

# --- filtres « ce n'est pas un label »
_JUNK_EXACT = {
    "not on label", "self-released", "self released", "none", "n/a", "unknown",
    "independent", "independant",
}
_JUNK_SUBSTR = [
    "(bmi)", "(ascap)", "(sesac)", "(prs)", "(gema)", "(sacem)", "(sabam)", "(buma",
    "distrokid", "cd baby", "cdbaby", "tunecore", "awal", "the orchard", "believe digital",
    "absolute label services", "fuga", "ingrooves", "symphonic distribution",
    "routenote", "amuse", "ditto music", "horus music", "label engine",
]
_JUNK_RE = re.compile(r"\bnot on label\b", re.I)


def normalize_label(name):
    n = (name or "").strip().lower()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def clean_label(name):
    """Récupère le vrai nom quand le parsing a laissé une chaîne de licence, un
    préfixe « Discogs: », etc. Retourne le nom nettoyé (ou "" si irrécupérable)."""
    s = (name or "").strip()
    s = re.sub(r"^\s*discogs\s*:\s*", "", s, flags=re.I)
    m = re.search(r"(?:licen[sc]e\s+(?:to|from)|distributed by|marketed by)\s+(.+)$", s, re.I)
    if m:
        s = m.group(1).strip()
    s = s.rstrip(" :;.-")
    if "http" in s.lower() or "_" in s or len(s) < 2:
        return ""
    return s


def is_junk(name):
    low = (name or "").strip().lower()
    if not low or low in _JUNK_EXACT or _JUNK_RE.search(low):
        return True
    return any(s in low for s in _JUNK_SUBSTR)


def load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    cfg = load(CONFIG_PATH, {})
    corpus = load(CORPUS_PATH, [])
    resolved = load(RESOLVED_PATH, {})
    base = cfg.get("labels", [])
    base_keys = {normalize_label(l) for l in base}

    # labels distincts du corpus (nom le plus fréquent conservé)
    from collections import Counter
    counts = Counter()
    display = {}
    dropped_raw = []
    for r in corpus:
        lab = clean_label(r.get("label"))
        if not lab:
            if r.get("label"):
                dropped_raw.append(r["label"])
            continue
        k = normalize_label(lab)
        counts[k] += 1
        display.setdefault(k, lab)

    junk = sorted(set(display[k] for k in display if is_junk(display[k])) | set(dropped_raw))
    good_keys = [k for k in display if not is_junk(display[k])]

    new_keys = [k for k in good_keys if k not in base_keys]
    new_names = sorted(display[k] for k in new_keys)

    print(f"Corpus : {len(display)} labels distincts")
    print(f"  - écartés (éditeurs/distributeurs/placeholders) : {len(junk)}")
    for j in junk:
        print(f"      · {j}")
    print(f"  - déjà dans la base : {len(good_keys) - len(new_keys)}")
    print(f"  - NOUVEAUX à ajouter : {len(new_names)}")
    for n in new_names[:40]:
        print(f"      + {n}  ({counts[normalize_label(n)]}x)")
    if len(new_names) > 40:
        print(f"      … +{len(new_names) - 40} autres")

    if DRY:
        print("\n--dry : rien écrit.")
        return

    # 1) base
    cfg["labels"] = base + new_names
    save(CONFIG_PATH, cfg)

    # 2) résolus : marque tous les labels du corpus (non junk) comme confirmés
    seeded = 0
    for k in good_keys:
        cur = resolved.get(k)
        if cur and cur.get("status") in ("exact", "confirmed"):
            continue
        resolved[k] = {"original": display[k], "discogs_name": display[k],
                       "discogs_id": (cur or {}).get("discogs_id"),
                       "status": "confirmed", "reviewed_by": "corpus"}
        seeded += 1
    save(RESOLVED_PATH, resolved)

    print(f"\nÉcrit : base {len(base)} -> {len(cfg['labels'])} labels "
          f"(+{len(new_names)}). {seeded} entrées résolues marquées 'confirmed' (corpus).")
    print("Relance l'appli Streamlit.")


if __name__ == "__main__":
    main()
