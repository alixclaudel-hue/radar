"""Cache partagé + cascade de clés pour l'API YouTube Data (Option A, étape 5a).

- Cache : /data/shared/youtube_cache.json (données publiques, neutres). Économise
  surtout les appels `search` (100 unités de quota chacun).
- Cascade : on essaie la clé perso de l'utilisateur puis la clé de l'appli
  (`YOUTUBE_API_KEY`) ; sur 403 quotaExceeded on passe à la suivante.
"""
import hashlib
import os
import re
import time

import requests

from . import paths, store
from .textmatch import overlap, toks

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
# Un échec n'est pas une vérité stable : il peut venir d'un quota épuisé, d'une
# indexation YouTube encore incomplète ou d'un scoring trop strict. Le mettre en
# cache aussi longtemps qu'un succès gèle la piste (incident du 2026-09-10 : une
# file entière rendue « aucune vidéo trouvée » pour 7 jours).
NEG_TTL = 6 * 3600
# Un succès, lui, est une vérité stable (le clip d'une piste ne change pas) : le
# recacher périodiquement ne sert qu'à reconsommer du quota de recherche pour
# retrouver le même résultat (retour utilisateur 2026-09-10 : « limiter les
# tokens de recherche », puis « je veux que cette base soit gardée en continu »
# — aucune expiration, pas juste une longue durée). Cache partagé (SHARED_DIR)
# entre tous les appelants (RECOS RADAR, /yt/first) et, à terme, tous les
# utilisateurs. Reste borné en taille (pas en âge) par cache_put : au-delà de
# 20000 entrées, les plus anciennes sont purgées — protection disque (cf.
# CLAUDE.md, piège backup/disque VPS), pas une expiration de fraîcheur.
DEFAULT_TTL = float("inf")


def _toks(s):
    return toks(s, _FILLER)


def _strip_parens(s):
    """Retire les segments entre parenthèses/crochets (« (Original Mix) »,
    « (feat. X) »…) : Discogs les met dans le titre de piste, mais ils
    n'apparaissent presque jamais tels quels dans les métadonnées YouTube — ils
    gonflaient `want` au dénominateur et faisaient chuter le score d'un match
    par ailleurs parfait (diagnostic 2026-09-10)."""
    return re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s or "")


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


def cache_get(key, max_age, empty_max_age=None):
    """Valeur en cache si elle est encore fraîche, sinon None.

    `empty_max_age` donne un âge maximal plus court aux valeurs vides (échec de
    recherche), qui ne valent pas d'être gardées aussi longtemps qu'un succès."""
    e = store.load(CACHE_PATH, {}).get(key)
    if not e:
        return None
    v = e.get("v")
    limit = max_age if v or empty_max_age is None else empty_max_age
    if (time.time() - e.get("ts", 0)) < limit:
        return v
    return None


def cache_put(key, value):
    d = store.load(CACHE_PATH, {})
    d[key] = {"ts": time.time(), "v": value}
    if len(d) > 20000:
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:5000]:
            d.pop(k, None)
    store.save(CACHE_PATH, d, indent=None)


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


def search_video(query, keys, ttl=DEFAULT_TTL, artist=None, title=None, label=""):
    """videoId de la meilleure vidéo pour `query`, ou None. Voir search_video_diag."""
    return search_video_diag(query, keys, ttl, artist, title, label)[0]


def search_video_diag(query, keys, ttl=DEFAULT_TTL, artist=None, title=None, label=""):
    """(videoId | None, raison d'échec) pour `query` parmi les 15 premiers résultats,
    en ne gardant qu'une vidéo lisible dont les métadonnées (titre, chaîne,
    description) recoupent le mieux `artist`/`title`/`label` (mise en cache).
    Enrichit la requête avec le label si disponible (très discriminant en musique
    électronique) ; si aucun bon match, retente sans le label (100 unités
    supplémentaires, seulement en cas d'échec). Quand `artist`/`title` sont fournis
    et qu'aucun résultat ne dépasse le seuil, renvoie None (une mauvaise vidéo est
    pire que pas de vidéo).

    La raison distingue « rien trouvé » de « seuil non atteint » : sans elle, un
    journal RECOS RADAR ne dit pas si la piste est introuvable ou si le scoring est
    trop strict (incident du 2026-09-10).

    QuotaExhausted remonte à l'appelant au lieu d'être avalée : le quota épuisé se
    lisait sinon « aucune vidéo trouvée », la file de candidats se vidait pour rien
    et l'échec était mis en cache comme un vrai résultat négatif."""
    q = " ".join((query or "").split())
    if not q:
        return None, "requête vide"
    if not keys:
        return None, "aucune clé YouTube"
    label_s = (label or "").strip()
    q_rich = f"{q} {label_s}" if label_s else q
    ckey = "search:" + hashlib.sha1(
        f"{q_rich}|{artist or ''}|{title or ''}|{label}".encode()
    ).hexdigest()[:20]
    hit = cache_get(ckey, ttl, NEG_TTL)
    if hit is not None:
        return (hit or None), ("" if hit else "aucun match, en cache")
    vid, why = "", "aucun résultat"
    seen = set()
    for attempt in (q_rich, q) if label_s else (q,):
        d = request("/search", {"part": "id", "type": "video",
                                "maxResults": 15, "q": attempt}, keys)
        ids = [((it.get("id") or {}).get("videoId") or "")
               for it in d.get("items", [])]
        ids = [i for i in ids if i and i not in seen]
        seen.update(ids)
        if ids:
            # `q` (sans label) et non `attempt` : le label aide le classement
            # YouTube mais ne doit pas peser dans le score (point F1, diagnostic
            # 2026-09-10 — sinon un label de 3 mots suffit à diluer `want` sous
            # le seuil pour un match par ailleurs parfait).
            vid, why = _best_match(ids, q, artist, title, label, keys)
            if vid:
                break
    cache_put(ckey, vid)
    return (vid or None), ("" if vid else why)


def _best_match(ids, query, artist, title, label, keys):
    """(meilleur id lisible, raison d'échec) parmi `ids`, selon recoupement des
    métadonnées (snippet : titre, chaîne, description) avec la piste demandée. Coût
    quota inchangé (1 unité pour `/videos` quel que soit le nombre de parts). Quand
    `artist`/`title` sont fournis et qu'aucun résultat ne dépasse MIN_MATCH_SCORE,
    renvoie "" (pas de repli permissif : une mauvaise vidéo est pire que pas de
    vidéo). Sans données structurées (/yt/first), repli sur le 1er résultat lisible."""
    structured = bool((artist or "").strip() or (title or "").strip())
    try:
        d = request("/videos", {"part": "snippet,status", "id": ",".join(ids)}, keys)
    except QuotaExhausted:
        raise
    except RuntimeError:
        return ("" if structured else ids[0]), "erreur API /videos"
    items = {it.get("id"): it for it in d.get("items", [])}

    def playable(i):
        it = items.get(i)
        if not it:
            return False
        st = it.get("status", {})
        return st.get("uploadStatus") == "processed" and st.get("privacyStatus") in ("public", "unlisted")

    # `want`/`t_toks` sur le titre "core" (sans mentions de version type
    # « (Original Mix) ») : ces mentions n'apparaissent presque jamais telles
    # quelles dans les métadonnées YouTube (point F3). `want_full` (avec)
    # reste utilisé pour la détection remix/live/edit ci-dessous, pour ne pas
    # pénaliser une vidéo qui porte justement la bonne mention de version.
    want_full = _toks(query)
    want = _toks(_strip_parens(query))
    a_toks = _toks(artist or "")
    t_toks = _toks(_strip_parens(title or ""))
    l_toks = _toks(label or "")
    # -1.0 et non 0.0 : un résultat scoré 0 doit quand même être retenu comme
    # « meilleur », pour que la raison d'échec cite la vidéo vue au lieu de laisser
    # croire qu'aucun résultat n'était comparable.
    best_id, best_score, best_title = None, -1.0, ""
    for i in ids:
        if not playable(i):
            continue
        sn = items[i].get("snippet", {})
        vt_toks = _toks(sn.get("title", ""))
        ch_toks = _toks(sn.get("channelTitle", ""))
        desc_toks = _toks((sn.get("description") or "")[:500])
        cand = vt_toks | ch_toks
        cand_wide = cand | desc_toks
        if not want or not cand:
            continue
        score = overlap(want, cand)
        desc_extra = (want - cand) & desc_toks
        if desc_extra:
            score += 0.1 * len(desc_extra) / len(want)
        if l_toks and (l_toks & ch_toks):
            score += 0.2
        if overlap(a_toks, ch_toks) >= 0.5:
            score += 0.1
        foreign = vt_toks - want_full
        if foreign & _ALT_VERSION:
            score *= 0.4
        # Les deux portes titre/artiste ne se cumulent plus (F4) : un ×0.5 puis
        # un second ×0.5 valait un veto de fait (×0.25) même sur un match par
        # ailleurs correct — un seul des deux signaux faibles suffit à motiver
        # la pénalité, pas les deux ensemble.
        gate_pens = []
        if t_toks and overlap(t_toks, vt_toks | desc_toks) < 0.4:
            gate_pens.append(0.5)
        if a_toks and overlap(a_toks, cand_wide) < 0.4:
            gate_pens.append(0.5)
        if gate_pens:
            score *= min(gate_pens)
        if score > best_score:
            best_id, best_score, best_title = i, score, sn.get("title", "")
    if best_id is not None and best_score >= MIN_MATCH_SCORE:
        return best_id, ""
    if not structured:
        for i in ids:
            if playable(i):
                return i, ""
        return ids[0], ""
    if best_id is None:
        return "", f"{len(ids)} résultat(s), aucun lisible"
    return "", f"meilleur score {best_score:.2f} < {MIN_MATCH_SCORE} : « {best_title} »"
