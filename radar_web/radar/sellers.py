"""Catalogue partagé de vendeurs Discogs + snapshots d'inventaire.

- `sellers_catalog.json` : {username: {name, country, city, focus, type, source,
  active, verified, n_items, n_new, last_scan, note, n_12in, n_matched,
  top_styles}} — donnée publique, mutualisée. `note`/`n_matched`/`top_styles`
  sont calculés par `seller_affinity()` à partir du goût du propriétaire.
- `seller_inventory/<username>.json` : {release_id: {listing_id, price, currency,
  condition, sleeve, listed, artist, format}} — snapshot « For Sale » du dernier
  scan. `artist`/`format` viennent gratuitement de l'inventaire Discogs (pas
  d'appel supplémentaire).

Aucun appel API ici : la lecture est instantanée. Le remplissage se fait par le
job `scan_catalog` (crate_jobs.py).
"""
import json
import os

from . import paths, sellers_seed
from .store import load_cached, normalize_label

_NOT_12IN = ('7"', '10"', "CD", "Cassette", "Cass", "File", "DVD")


def _is_12in(fmt):
    """Heuristique sur la chaîne `format` de Discogs (ex. '12", EP', 'LP, Album, RE',
    '7", Single') : exclut explicitement les formats non-12", le reste (LP ou 12"
    explicite) est du 12"."""
    f = fmt or ""
    if any(x in f for x in _NOT_12IN):
        return False
    return "LP" in f or '12"' in f


def seller_affinity(inv, ctx):
    """Aperçu goût d'un vendeur sur son stock 12" uniquement (pas les 45 tours) :
    note = affinité moyenne (façon album_score) des artistes déjà connus dans
    `ctx.ascore`, sur les articles où au moins un artiste crédité est reconnu.
    `top_styles` : genre réel du référentiel local (radar/discogs_dump.py,
    construit depuis le dump Discogs) quand la sortie y est cataloguée ; à
    défaut (pas encore importé, ou sortie trop récente/absente du dump),
    repli sur les labels déjà profilés par l'utilisateur — pas d'appel API
    supplémentaire dans les deux cas."""
    from . import discogs_dump as dd
    bl = float(ctx.scoring["album"].get("artist_max_vs_mean", 0.6))
    item_scores, style_hits, n_12in = [], {}, 0
    con = dd.connect_readonly()
    try:
        for rid, item in inv.items():
            if not _is_12in(item.get("format")):
                continue
            n_12in += 1
            arts = ctx.split_credit_artists(item.get("artist") or "")
            known = [ctx.ascore[k] for a in arts
                     if (k := ctx.canon_artist_key(a)) in ctx.ascore]
            if known:
                item_scores.append(bl * max(known) + (1 - bl) * (sum(known) / len(known)))
            ref = dd.lookup_release(rid, con) if con else None
            styles = (ref or {}).get("styles")
            if styles:
                for s in styles.split(", "):
                    if s:
                        style_hits[s] = style_hits.get(s, 0) + 1
            else:
                lab = item.get("label")
                prof = ctx.profile.get(normalize_label(lab)) if lab else None
                for s, n in ((prof or {}).get("style_counts") or {}).items():
                    style_hits[s] = style_hits.get(s, 0) + n
    finally:
        if con:
            con.close()
    return {"n_12in": n_12in,
            "n_matched": len(item_scores),
            "note": round(sum(item_scores) / len(item_scores)) if item_scores else None,
            "top_styles": sorted(style_hits, key=style_hits.get, reverse=True)[:3]}

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


INDEX_PATH = os.path.join(paths.SHARED_DIR, "seller_index.json")


def load_index():
    return load_cached(INDEX_PATH, {})


def save_index(idx):
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, INDEX_PATH)


def update_index(idx, username, inv):
    """Met à jour idx (`{release_id: [username, …]}`) en place pour un vendeur :
    retire ses anciennes entrées, les réajoute d'après le nouveau snapshot."""
    for rid, users in list(idx.items()):
        if username in users:
            users.remove(username)
            if not users:
                del idx[rid]
    for rid in inv:
        idx.setdefault(rid, [])
        if username not in idx[rid]:
            idx[rid].append(username)


def rebuild_index(usernames):
    """Reconstruction complète (backfill initial, ou si l'index a été perdu) —
    relit tous les inventaires. Coûteux (28 Mo) : à réserver au job de fond."""
    idx = {}
    for u in usernames:
        update_index(idx, u, load_inventory(u))
    return idx


def cart_coverage(cart_ids):
    """[(username, entry, [release_id présents])] trié par couverture, via
    l'index inversé (pas de lecture des inventaires détaillés)."""
    want = {str(x) for x in cart_ids}
    if not want:
        return []
    cat = load_catalog()
    idx = load_index()
    hits_by_user = {}
    for rid in want:
        for u in idx.get(rid, []):
            hits_by_user.setdefault(u, []).append(rid)
    out = []
    for u, hits in hits_by_user.items():
        e = cat.get(u)
        if not e or not e.get("active"):
            continue
        out.append((u, e, hits))
    out.sort(key=lambda t: (-len(t[2]), t[1].get("name", t[0]).lower()))
    return out
