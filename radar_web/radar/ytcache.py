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
from .textmatch import overlap, overlap_fuzzy, toks

CACHE_PATH = os.path.join(paths.SHARED_DIR, "youtube_cache.json")
API = "https://www.googleapis.com/youtube/v3"
# Deux familles d'erreurs bien distinctes, longtemps confondues sous un même
# quota "épuisé" (diagnostic VPS 16/09) : quotaExceeded/dailyLimitExceeded sont
# le quota des 10 000 unités/jour (persistant, ne se résout qu'au lendemain) ;
# rateLimitExceeded/userRateLimitExceeded sont NORMALEMENT une limite de débit
# par seconde (transitoire, se résorbe en quelques secondes) — preuve en réel :
# un appel rejeté "quota" a réussi identiquement 20s plus tard avec la MÊME clé.
#
# CONTRE-INTUITIF, 2e diagnostic VPS en 24h en sens inverse du 1er : Google
# renvoie AUSSI reason="rateLimitExceeded" pour un quota JOURNALIER bien précis
# ("Search Queries per day", distinct des 10 000 unités/jour globales — /videos
# peut répondre 200 au même instant où /search est épuisé). Exemple réel :
#   429 {"error": {"message": "Quota exceeded for quota metric 'Search
#   Queries' and limit 'Search Queries per day' of service
#   'youtube.googleapis.com' for consumer '...'",
#   "errors": [{"reason": "rateLimitExceeded"}]}}
# La `reason` ment ici ; seul le `message` tranche (cf. _error_kind). Les deux
# cas sont donc réels et symétriques : rateLimitExceeded SANS mention "per day"
# dans le message reste transitoire (PR #158) ; AVEC, c'est un quota journalier
# (PR suivante) — ne pas re-fusionner les deux familles sur un seul indice.
_DAILY_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded"}
_RATE_LIMIT_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}
# Marqueur de quota JOURNALIER dans le `message` d'une erreur par ailleurs
# classée _RATE_LIMIT_REASONS (cf. commentaire ci-dessus) — pas dans `reason`,
# qui ment précisément dans ce cas.
# UNIQUEMENT la fenêtre de la limite ("per day"), surtout pas le préfixe
# "Quota exceeded for quota metric ...", commun à TOUTES les métriques : Google
# l'emploie aussi pour les limites transitoires ("... and limit 'Queries per
# minute per user' ..."), qu'il ferait alors passer à tort pour un quota
# journalier — soit exactement le bug corrigé par la PR #158, réintroduit par
# l'autre bout. Toute autre fenêtre (minute, 100 secondes) reste transitoire
# par défaut, donc traitée en "rate".
_DAILY_QUOTA_MESSAGE_MARKERS = ("per day",)
# Backoff sur limite de débit transitoire, sur la MÊME clé, avant de basculer
# sur la suivante ou d'abandonner.
_RATE_LIMIT_RETRY_DELAYS = (1, 2, 4)
# Espacement minimal entre deux appels HTTP à l'API (toutes clés confondues) :
# aucun throttle n'existait avant, et les relances de candidats en échec
# partent avec force=True (crate_jobs.job_publish_recos) donc en rafale —
# exactement ce qui déclenche la limite de débit ci-dessus.
_MIN_CALL_INTERVAL = 0.34  # ~3 req/s
_last_call_ts = 0.0

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


class RateLimited(RuntimeError):
    """Limite de débit YouTube (par seconde/utilisateur) après épuisement des
    retries -- distincte de QuotaExhausted : transitoire, pas le quota
    journalier. L'appelant doit reprendre plus tard, pas abandonner tout le
    run (cf. crate_jobs.job_publish_recos)."""
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


def _error_message(resp):
    """`error.message` de la réponse Google, en minuscules -- c'est lui, pas
    `reason`, qui distingue un quota "Search Queries per day" d'une vraie
    limite de débit (cf. _DAILY_QUOTA_MESSAGE_MARKERS)."""
    try:
        return (resp.json().get("error", {}).get("message") or "").lower()
    except ValueError:
        return resp.text.lower()


def _error_kind(r):
    """"daily" (quota journalier, persistant), "rate" (limite de débit,
    transitoire) ou None (pas une erreur de quota).

    Le repli sur le texte "quota" (nouveau format 429 sans `reason`
    exploitable, cf. retour utilisateur du 09/09, `Quota Queries per day`) ne
    s'applique QUE quand Google ne renvoie aucune `reason` du tout -- resserré
    le 16/09 : il rattrapait avant n'importe quel 403/429 dont le message
    contenait incidemment ce mot, y compris une vraie limite de débit.

    Une `reason` dans _RATE_LIMIT_REASONS n'est PAS toujours transitoire pour
    autant (2e diagnostic VPS, même jour) : Google l'utilise aussi pour un
    quota journalier précis ("Search Queries per day"), où la `reason` ment --
    seul le `message` le révèle. D'où la vérification `message` avant de
    conclure "rate", cf. le commentaire de _DAILY_QUOTA_MESSAGE_MARKERS."""
    if r.status_code not in (403, 429):
        return None
    reasons = _reason(r)
    if reasons & _DAILY_QUOTA_REASONS:
        return "daily"
    if reasons & _RATE_LIMIT_REASONS:
        message = _error_message(r)
        if any(m in message for m in _DAILY_QUOTA_MESSAGE_MARKERS):
            return "daily"
        return "rate"
    if not reasons and "quota" in r.text.lower():
        return "daily"
    return None


def _throttle():
    """Espace les appels HTTP d'au moins _MIN_CALL_INTERVAL, toutes clés/threads
    confondus -- mais PAR PROCESSUS seulement (`_last_call_ts` est un global
    de module) : garanti à l'intérieur du worker sériel (worker.py), PAS entre
    le worker et le conteneur web (`radar_web/app.py::/yt/first` tourne dans
    un processus distinct avec son propre `_last_call_ts`) -- diagnostic VPS
    16/09, BUG 4. Les deux peuvent donc partir en rafale l'un contre l'autre.
    Un partage réel demanderait un marqueur dans SHARED_DIR + un verrou
    fichier entre processus -- pas fait ici, à arbitrer si ça devient un
    problème mesuré en pratique."""
    global _last_call_ts
    wait = _last_call_ts + _MIN_CALL_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def _get(path, params, key, timeout):
    """GET sur une clé, avec retries + backoff sur limite de débit transitoire
    (jusqu'à _RATE_LIMIT_RETRY_DELAYS tentatives sur la MÊME clé avant de la
    considérer épuisée pour ce cycle)."""
    _throttle()
    r = requests.get(f"{API}{path}", params={**params, "key": key}, timeout=timeout)
    for delay in _RATE_LIMIT_RETRY_DELAYS:
        if r.ok or _error_kind(r) != "rate":
            break
        time.sleep(delay)
        _throttle()
        r = requests.get(f"{API}{path}", params={**params, "key": key}, timeout=timeout)
    return r


def request(path, params, keys, timeout=15):
    """GET {API}{path} en essayant chaque clé. QuotaExhausted si toutes en quota
    journalier ; RateLimited si toutes sont seulement en limite de débit
    (transitoire) après retries -- l'appelant doit réessayer plus tard, pas
    considérer la journée perdue (cf. RateLimited)."""
    last, any_rate = None, False
    for k in keys:
        r = _get(path, params, k, timeout)
        if r.ok:
            return r.json()
        last = r
        kind = _error_kind(r)
        if kind == "daily":
            continue                      # clé épuisée pour la journée -> suivante
        if kind == "rate":
            any_rate = True
            continue                      # retries épuisés sur cette clé -> suivante
        raise RuntimeError(f"YouTube {r.status_code}: {r.text[:200]}")
    if last is not None:
        # `any_rate` AVANT le quota journalier, et non la seule dernière réponse :
        # la journée n'est perdue que si AUCUNE clé ne peut plus servir aujourd'hui.
        # Une clé simplement limitée en débit redevient utilisable en quelques
        # secondes ; conclure QuotaExhausted parce que la DERNIÈRE clé essayée est
        # épuisée pour la journée abandonnerait tout le run (et renverrait
        # l'utilisateur à la remise à zéro du quota) alors qu'une autre clé
        # repasserait au prochain lancement. Sans effet tant qu'une seule clé est
        # configurée ; compte dès qu'il y en a deux.
        if any_rate:
            raise RateLimited("Limite de débit YouTube atteinte sur toutes les clés "
                              "encore utilisables aujourd'hui, après plusieurs "
                              "tentatives -- réessaie dans quelques minutes.")
        if _error_kind(last) == "daily":
            raise QuotaExhausted("Quota YouTube épuisé (toutes les clés). "
                                 "Réessaie demain ou ajoute ta clé perso dans « Mon profil ».")
    raise RuntimeError("Aucune clé YouTube utilisable.")


def search_video(query, keys, ttl=DEFAULT_TTL, artist=None, title=None, label="", force=False):
    """videoId de la meilleure vidéo pour `query`, ou None. Voir search_video_diag."""
    return search_video_diag(query, keys, ttl, artist, title, label, force)[0]


def search_video_diag(query, keys, ttl=DEFAULT_TTL, artist=None, title=None, label="", force=False):
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
    et l'échec était mis en cache comme un vrai résultat négatif.

    `force` ignore un résultat négatif déjà en cache pour retenter une vraie
    recherche (un résultat positif reste toujours servi tel quel, il ne coûte
    rien de le réutiliser). Sans ça, les relances de `job_publish_recos` sur un
    candidat déjà tenté (point 33 CLAUDE.md, `RECOS_MAX_ATTEMPTS`) ne faisaient
    que relire le même échec mis en cache jusqu'à 6h plus tôt (`NEG_TTL`) sans
    jamais réinterroger l'API — les 3 tentatives ne cherchaient donc rien de
    nouveau, quel que soit un correctif de scoring déployé entre-temps (retour
    utilisateur 2026-09-10 : « aucun match, en cache » sur des pistes retrouvées
    en 2 clics à la main)."""
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
    if hit:
        return hit, ""
    if hit is not None and not force:
        return None, "aucun match, en cache"
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


_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    """Identifiant de vidéo contenu dans `url`, ou "" si ce n'en est pas une.

    Les vidéos attachées à une sortie Discogs (`release["videos"][]["uri"]`) sont
    presque toujours des liens YouTube, mais sous plusieurs graphies (watch?v=,
    youtu.be, embed). L'appli, elle, ne manipule que l'identifiant : la playlist
    RECOS RADAR le stocke (`video_id`) et le lecteur IFrame ne lit que ça."""
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _is_playable(item):
    """Une entrée de `/videos` est-elle lisible par un visiteur ? (traitée, et
    publique ou non listée). Partagé par `_best_match` et `playable_video` : une
    vidéo privée, supprimée ou encore en cours de traitement ne doit jamais
    entrer dans la playlist, quelle que soit sa provenance."""
    st = (item or {}).get("status", {})
    return st.get("uploadStatus") == "processed" and st.get("privacyStatus") in ("public", "unlisted")


def playable_video(vid, keys):
    """La vidéo `vid` est-elle lisible ? Pour valider un identifiant qu'on tient
    déjà d'ailleurs (vidéo attachée à une sortie Discogs) plutôt que d'une
    recherche : 1 unité de quota (`/videos`) au lieu d'environ 101 pour une
    recherche complète, et cette métrique-là reste servie quand le quota de
    RECHERCHE de la journée est épuisé (cf. point 52 de CLAUDE.md). `False` en
    cas d'erreur API non classée : sans confirmation, on ne publie pas.
    `QuotaExhausted`/`RateLimited` remontent à l'appelant, comme ailleurs."""
    if not vid:
        return False
    try:
        d = request("/videos", {"part": "status", "id": vid}, keys)
    except (QuotaExhausted, RateLimited):
        raise
    except RuntimeError:
        return False
    return any(_is_playable(it) for it in d.get("items", []) if it.get("id") == vid)


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
    except (QuotaExhausted, RateLimited):
        raise
    except RuntimeError:
        return ("" if structured else ids[0]), "erreur API /videos"
    items = {it.get("id"): it for it in d.get("items", [])}

    def playable(i):
        return _is_playable(items.get(i))

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
            # `_strip_parens` peut réduire le titre à 1-2 jetons : sans autre
            # mot pour se rattraper, la moindre faute de frappe côté YouTube
            # (apostrophe, lettre doublée) fait chuter `overlap` à 0.0 pile sur
            # un match par ailleurs correct (diagnostic 2026-09-19, cas réel
            # « Stepin' » vs « Steppin' »). Tolérance bornée à ce cas précis :
            # jamais sur un titre à 3 jetons ou plus, qui a d'autres mots pour
            # prouver le match sans avoir besoin de cette marge.
            if len(t_toks) <= 2 and overlap_fuzzy(t_toks, vt_toks | desc_toks) >= 0.4:
                pass
            else:
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
