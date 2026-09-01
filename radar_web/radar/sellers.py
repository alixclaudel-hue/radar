"""Catalogue partagé de vendeurs Discogs + snapshots d'inventaire.

- `sellers_catalog.json` : {username: {name, country, city, focus, type, source,
  active, verified, n_items, n_new, last_scan}} — donnée publique, mutualisée.
- `seller_inventory/<username>.json` : {release_id: {listing_id, price, currency,
  condition, sleeve, listed}} — snapshot « For Sale » du dernier scan.

Aucun appel API ici : la lecture est instantanée. Le remplissage se fait par le
job `scan_catalog` (crate_jobs.py).
"""
import json
import os

from . import paths, sellers_seed

CATALOG_PATH = os.path.join(paths.SHARED_DIR, "sellers_catalog.json")
INV_DIR = os.path.join(paths.SHARED_DIR, "seller_inventory")


def load_catalog():
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def save_catalog(d):
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    tmp = CATALOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CATALOG_PATH)


def ensure_seeded():
    """Ajoute les entrées du socle absentes du catalogue. Idempotent."""
    cat = load_catalog()
    changed = False
    for s in sellers_seed.SEED:
        if s["u"] not in cat:
            cat[s["u"]] = {"name": s["name"], "country": s["c"], "city": s["city"],
                           "focus": s["f"], "type": s["t"], "source": "seed",
                           "active": True, "verified": None,
                           "n_items": None, "n_new": None, "last_scan": None}
            changed = True
    if changed:
        save_catalog(cat)
    return cat


def inv_file(username):
    return os.path.join(INV_DIR, username.replace("/", "_") + ".json")


def load_inventory(username):
    try:
        with open(inv_file(username), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def save_inventory(username, data):
    os.makedirs(INV_DIR, exist_ok=True)
    tmp = inv_file(username) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, inv_file(username))


def cart_coverage(cart_ids):
    """[(username, entry, [release_id présents], top_prix)] trié par couverture."""
    want = {str(x) for x in cart_ids}
    if not want:
        return []
    cat = load_catalog()
    out = []
    for u, e in cat.items():
        if not e.get("active"):
            continue
        inv = load_inventory(u)
        hits = [rid for rid in want if rid in inv]
        if hits:
            out.append((u, e, hits))
    out.sort(key=lambda t: (-len(t[2]), t[1].get("name", t[0]).lower()))
    return out
