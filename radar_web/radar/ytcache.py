"""Cache partagé + cascade de clés pour l'API YouTube Data (Option A, étape 5a).

- Cache : /data/shared/youtube_cache.json (données publiques, neutres). Économise
  surtout les appels `search` (100 unités de quota chacun).
- Cascade : on essaie la clé perso de l'utilisateur puis la clé de l'appli
  (`YOUTUBE_API_KEY`) ; sur 403 quotaExceeded on passe à la suivante.
"""
import hashlib
import json
import os
import re
import time

import requests

from . import paths

CACHE_PATH = os.path.join(paths.SHARED_DIR, "youtube_cache.json")
API = "https://www.googleapis.com/youtube/v3"
_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded", "ratelimitexceeded",
                  "userratelimitexceeded"}

# Pertinence du résultat (retour utilisateur 2026-09-10 : plus de la moitié des
# vidéos ajoutées à RECOS RADAR n'avaient aucun rapport avec la piste demandée —
# search_video prenait le 1er résultat de recherche sans vérifier qu'il
# correspondait vraiment, contrairement à bandcamp.py/volumo.py qui scorent par
# recouvrement de mots. Même principe ici, appliqué au titre/à la chaîne de la
# vidéo (part `snippet`) — mots vides et mots trahissant une autre version que
# celle demandée (remix, live, cover…).
_FILLER = {"pt", "part", "i", "ii", "iii", "iv", "v", "vi", "the", "a", "an",
           "ft", "feat", "featuring", "and"}
_ALT_VERSION = {"remix", "rework", "bootleg", "edit", "flip", "refix", "vip",
                "reprise", "mashup", "cover", "live", "tutorial", "lesson",
                "reaction", "review", "lyrics", "karaoke", "instrumental",
                "acapella", "sped", "slowed", "nightcore", "8d", "loop"}
MIN_MATCH_SCORE = 0.5


def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - _FILLER


class QuotaExhausted(RuntimeError):
    pass


def youtube_keys(cfg):
    """Clés à essayer, dans l'ordre : perso (protège le pot commun) puis appli."""
    out = []
    for k in ((cfg or {}).get("youtube_api_key"), os.environ.get("YOUTUBE_API_KEY")):
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CACHE_PATH)


def cache_get(key, max_age):
    e = _load().get(key)
    if e and (time.time() - e.get("ts", 0)) < max_age:
        return e.get("v")
    return None


def cache_put(key, value):
    d = _load()
    d[key] = {"ts": time.time(), "v": value}
    if len(d) > 20000:
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:5000]:
            d.pop(k, None)
    _save(d)


def _reason(resp):
    try:
        errs = resp.json().get("error", {}).get("errors", [])
        return {e.get("reason", "").lower() for e in errs}
    except ValueError:
        return set()


def _is_quota_response(r):
    """Quota épuisé, sous deux formats Google vus en réel : l'ancien (403,
    `error.errors[].reason` dans _QUOTA_REASONS) et le nouveau (429, juste
    `error.code`/`error.message`, repéré au texte "quota" du message — cf.
    retour utilisateur du 09/09, `Quota Queries per day` en 429)."""
    if r.status_code not in (403, 429):
        return False
    if _reason(r) & _QUOTA_REASONS:
        return True
    return "quota" in r.text.lower()


def request(path, params, keys, timeout=15):
    """GET {API}{path} en essayant chaque clé. QuotaExhausted si toutes en quota."""
    last = None
    for k in keys:
        r = requests.get(f"{API}{path}", params={**params, "key": k}, timeout=timeout)
        if r.ok:
            return r.json()
        last = r
        if _is_quota_response(r):
            continue                      # clé épuisée -> suivante
        raise RuntimeError(f"YouTube {r.status_code}: {r.text[:200]}")
    if last is not None and _is_quota_response(last):
        raise QuotaExhausted("Quota YouTube épuisé (toutes les clés). "
                             "Réessaie demain ou ajoute ta clé perso dans « Mes sources ».")
    raise RuntimeError("Aucune clé YouTube utilisable.")


def search_video(query, keys, ttl=7 * 86400, artist=None, title=None, label=""):
    """videoId de la MEILLEURE vidéo pour `query` parmi les 5 premiers résultats,
    encore lisible ET dont les métadonnées (titre, chaîne) recoupent le mieux
    `artist`/`title`/`label` (mise en cache). `artist`/`title`/`label` sont
    optionnels (repli sur un simple recoupement contre les mots de `query`
    quand absents, cf. /yt/first qui n'a qu'une chaîne de recherche déjà
    composée). None si rien / pas de clé."""
    q = " ".join((query or "").split())
    if not q or not keys:
        return None
    ckey = "search:" + hashlib.sha1(f"{q}|{artist or ''}|{title or ''}|{label}".encode()).hexdigest()[:20]
    hit = cache_get(ckey, ttl)
    if hit is not None:
        return hit or None
    d = request("/search", {"part": "id", "type": "video", "maxResults": 5, "q": q}, keys)
    ids = [((it.get("id") or {}).get("videoId") or "") for it in d.get("items", [])]
    ids = [i for i in ids if i]
    vid = _best_match(ids, q, artist, title, label, keys) if ids else ""
    cache_put(ckey, vid)
    return vid or None


def _best_match(ids, query, artist, title, label, keys):
    """1er id lisible parmi `ids` (ordre de pertinence YouTube), en priorité celui
    dont les métadonnées (part `snippet`) recoupent le mieux la piste demandée —
    coût quota inchangé : `snippet` est demandé dans la MÊME requête `/videos`
    que la vérification de lisibilité existante (part `status`), qui coûte 1
    unité quel que soit le nombre de parts demandées (contrairement à `search`,
    100 unités). Repli sur le 1er id encore lisible si rien ne dépasse le seuil
    de recoupement (métadonnées pauvres côté vidéo — faux négatif préférable à
    aucune vidéo du tout) ou si l'appel `/videos` échoue."""
    try:
        d = request("/videos", {"part": "snippet,status", "id": ",".join(ids)}, keys)
    except (QuotaExhausted, RuntimeError):
        return ids[0]
    items = {it.get("id"): it for it in d.get("items", [])}

    def playable(i):
        it = items.get(i)
        if not it:
            return False
        st = it.get("status", {})
        return st.get("uploadStatus") == "processed" and st.get("privacyStatus") in ("public", "unlisted")

    want = _toks(query)
    a_toks, t_toks, l_toks = _toks(artist or ""), _toks(title or ""), _toks(label or "")
    best_id, best_score = None, 0.0
    for i in ids:
        if not playable(i):
            continue
        sn = items[i].get("snippet", {})
        vt_toks = _toks(sn.get("title", ""))
        ch_toks = _toks(sn.get("channelTitle", ""))
        cand = vt_toks | ch_toks
        if not want or not cand:
            continue
        score = len(want & cand) / len(want)
        if l_toks and (l_toks & ch_toks):          # chaîne officielle du label suivi
            score += 0.15
        foreign = vt_toks - want
        if foreign & _ALT_VERSION:                 # remix/live/cover... non demandé
            score *= 0.4
        if t_toks and len(t_toks & vt_toks) / len(t_toks) < 0.4:
            score *= 0.5                           # le cœur du titre doit être dans la vidéo
        if a_toks and len(a_toks & cand) / len(a_toks) < 0.4:
            score *= 0.5                           # l'artiste doit être dans le titre OU la chaîne
        if score > best_score:
            best_id, best_score = i, score
    if best_id is not None and best_score >= MIN_MATCH_SCORE:
        return best_id
    for i in ids:                                  # repli : ordre de pertinence YouTube
        if playable(i):
            return i
    return ids[0]
