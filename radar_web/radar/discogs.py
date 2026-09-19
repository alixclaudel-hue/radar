"""Client API Discogs — porté de crate_radar.py."""
import time

import requests

UA = "Radar/1.0 +personal-use"
BASE = "https://api.discogs.com"


class DiscogsError(RuntimeError):
    pass


def _check(r):
    if r.status_code == 401:
        raise DiscogsError("Token Discogs invalide ou manquant (401).")
    if r.status_code == 429:
        raise DiscogsError("Limite Discogs atteinte (429) — patiente une minute.")
    if not r.ok:
        raise DiscogsError(f"Erreur Discogs {r.status_code} : {r.text[:200]}")


def get(path, params=None, token=""):
    params = dict(params or {})
    if token:
        params["token"] = token
    r = requests.get(f"{BASE}{path}", params=params,
                     headers={"User-Agent": UA}, timeout=20)
    _check(r)
    return r.json()


def put(path, params=None, token=""):
    params = dict(params or {})
    if token:
        params["token"] = token
    r = requests.put(f"{BASE}{path}", params=params,
                     headers={"User-Agent": UA}, timeout=20)
    _check(r)
    return r.json() if r.text else {}


def delete(path, params=None, token=""):
    params = dict(params or {})
    if token:
        params["token"] = token
    r = requests.delete(f"{BASE}{path}", params=params,
                        headers={"User-Agent": UA}, timeout=20)
    _check(r)


def identity(token):
    return get("/oauth/identity", token=token)


def wants(token, username, page=1, per_page=100):
    return get(f"/users/{username}/wants", {"page": page, "per_page": per_page}, token=token)


def add_to_wantlist(token, username, release_id):
    put(f"/users/{username}/wants/{release_id}", token=token)


def remove_from_wantlist(token, username, release_id):
    delete(f"/users/{username}/wants/{release_id}", token=token)


def seller_inventory(username, token="", max_pages=10, per_page=100, on_page=None):
    """([{release_id, price, currency, condition, sleeve, artist, format, listing_id}],
    tronque) — articles « For Sale » d'un vendeur, pagination suivie jusqu'à
    `max_pages`. `tronque` dit que le vendeur a encore du stock au-delà, pour que
    l'appelant puisse le signaler au lieu de laisser croire à un inventaire complet.

    `max_pages=0` retire le plafond : TOUT le stock, quel que soit le nombre de
    pages (demande utilisateur 2026-09-19, cf. point 68 de CLAUDE.md). Compter
    ~100 articles et 1,1 s par page — un gros disquaire prend plusieurs minutes,
    d'où le job de fond `seller_inventory` plutôt qu'un appel dans une requête
    HTTP.

    `on_page(n_items, page, pages)` est appelé après chaque page, pour
    l'avancement ; renvoyer `False` interrompt la pagination proprement (bouton
    « arrêter » du job) et marque le résultat comme tronqué.

    Un compte inconnu ou sans boutique lève `DiscogsError` via `_check` (404) —
    pas de retour vide silencieux, l'utilisateur doit savoir qu'il s'est trompé de
    nom. Les champs `artist`/`format` viennent de la réponse, sans appel
    supplémentaire ; tout le reste (styles, genres, année, label) est ensuite lu
    dans le référentiel local par `release_id`.

    Volontairement indépendant de `sellers.py` et du catalogue de vendeurs, en
    pause depuis le 2026-09-17 (cf. point 56 de CLAUDE.md) : ici l'utilisateur
    nomme un vendeur à la volée et veut son stock du moment, pas un instantané
    daté d'un scan de fond."""
    out, page = [], 1
    while not max_pages or page <= max_pages:
        d = get(f"/users/{username}/inventory",
                {"status": "For Sale", "per_page": per_page, "page": page,
                 "sort": "listed", "sort_order": "desc"}, token=token)
        for x in d.get("listings", []):
            rel = x.get("release") or {}
            rid = rel.get("id")
            if not rid:
                continue
            pr = x.get("price") or {}
            out.append({"release_id": int(rid), "listing_id": x.get("id"),
                        "price": pr.get("value"), "currency": pr.get("currency"),
                        "condition": x.get("condition"),
                        "sleeve": x.get("sleeve_condition"),
                        "listed": x.get("posted"),
                        "artist": rel.get("artist"), "format": rel.get("format"),
                        "title": rel.get("title")})
        pages = d.get("pagination", {}).get("pages", 1)
        if on_page and on_page(len(out), page, pages) is False:
            return out, page < pages
        if page >= pages:
            return out, False
        page += 1
        time.sleep(1.1)                   # 60 requêtes/min côté Discogs
    return out, True


def search(token="", **params):
    params.setdefault("type", "release")
    return get("/database/search", params, token=token)


def release(release_id, token=""):
    return get(f"/releases/{release_id}", token=token)


def search_label_releases(token, label, genre="", style="", fmt="", year="", max_pages=3):
    """Sorties d'un label filtrées (pagination). Retourne la liste brute des résultats."""
    return _paged_release_search(token, "label", label, genre, style, fmt, year, max_pages)


def search_artist_releases(token, artist, genre="", style="", fmt="", year="", max_pages=3):
    """Sorties créditées à un artiste (pagination). Liste brute des résultats."""
    return _paged_release_search(token, "artist", artist, genre, style, fmt, year, max_pages)


def _paged_release_search(token, field, value, genre="", style="", fmt="", year="", max_pages=3):
    out, page = [], 1
    while page <= max_pages:
        p = {field: value, "per_page": 100, "page": page,
             "sort": "year", "sort_order": "desc"}
        for k, v in (("genre", genre), ("style", style), ("format", fmt), ("year", year)):
            if v:
                p[k] = v
        d = search(token=token, **p)
        res = d.get("results", [])
        out += res
        if page >= d.get("pagination", {}).get("pages", 1) or not res:
            break
        page += 1
        time.sleep(1.1)
    return out
