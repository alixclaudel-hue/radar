"""Chaîne de notation — portée de crate_radar.py, sans dépendance Streamlit.
Un objet Ctx charge toutes les données une fois ; les fonctions le prennent en argument.

v0 : label + style complets ; terme « artiste » simplifié (liste manuelle + corpus +
collection ; le graphe de producteurs viendra ensuite)."""
import re

from . import paths, store
from .store import normalize_label, style_key

ARTIST_STOPWORDS = {
    "various artists", "various", "va", "unknown artist", "unknown", "release",
    "progressive classics", "no artist", "traxsource", "dj", "self-released",
}
_CREDIT_SPLIT = re.compile(r"\s*(?:,|&| feat\.? | ft\.? | vs\.? | and | x | with )\s*", re.I)
_SIDE_MARKER = re.compile(
    r"^\s*(this|logo|flip|reverse|other|blank|etched|runout)?\s*side\b"
    r"|^\s*side\s*[a-d]{1,2}\s*$|^\s*[a-d]{1,2}\s*$", re.I)


class Ctx:
    """Instantané des données. Recréer à chaque requête (peu coûteux, fichiers < 3 Mo)."""

    def __init__(self, uid=None):
        self.uid = uid or store.current_uid()
        self.P = paths.user_paths(self.uid)
        self.cfg = store.load_config(self.uid)
        self.scoring = self.cfg["scoring"]
        # gros fichiers relus à chaque requête -> cache invalidé sur mtime
        self.profile = store.load_cached(self.P.profile, {})
        self.collection = store.load_cached(self.P.collection, {})
        self.corpus = store.load_cached(self.P.corpus, [])
        self.resolved = store.load_cached(self.P.resolved, {})
        self.artists_res = store.load_cached(self.P.artists_res, {})
        self.graph = store.load_cached(self.P.graph, {})
        self._wmap = None
        self._reco_idx = None
        self._ascore = None
        self._graph_rs = None

    # -------------------------------------------------------------- goût / styles
    @property
    def wmap(self):
        if self._wmap is None:
            cats = self.cfg.get("taste_categories", store.DEFAULT_TASTE_CATEGORIES)
            w = self.scoring["taste_tiers"]
            m = {}
            for cid, styles in cats.items():
                wc = float(w.get(cid, 0))
                for s in styles:
                    m[style_key(s)] = wc
            self._wmap = m
        return self._wmap

    def affinity_score(self, entry):
        sc = (entry or {}).get("style_counts") or {}
        total = sum(sc.values())
        if not total:
            return 0
        weighted = sum(n * self.wmap.get(style_key(s), 0.0) for s, n in sc.items())
        return min(100, round(100 * weighted / total))

    def style_affinity_of(self, styles):
        styles = [s for s in (styles or []) if s]
        if not styles:
            return None
        return min(100, round(100 * sum(self.wmap.get(style_key(s), 0.0) for s in styles) / len(styles)))

    # -------------------------------------------------------------- artistes
    def canon_artist_key(self, name):
        e = self.artists_res.get(normalize_label(name))
        if e and e.get("discogs_id") and e.get("status") in ("exact", "approx", "confirmed"):
            return f"id:{e['discogs_id']}"
        return normalize_label(name)

    def canon_artist_name(self, name):
        e = self.artists_res.get(normalize_label(name))
        if e and e.get("discogs_name") and e.get("status") in ("exact", "confirmed"):
            return e["discogs_name"]
        return name

    def artist_tier_map(self):
        m = {}
        for cid, names in self.cfg.get("artist_categories", {}).items():
            for n in names:
                m[self.canon_artist_key(n)] = cid
        return m

    def artist_disp(self):
        """{clé canonique: nom d'affichage} depuis toutes les sources."""
        d = {}
        for cid, names in self.cfg.get("artist_categories", {}).items():
            for n in names:
                d.setdefault(self.canon_artist_key(n), self.canon_artist_name(n))
        for r in self.corpus:
            a = (r.get("artist") or "").strip()
            if a:
                d.setdefault(self.canon_artist_key(a), self.canon_artist_name(a))
        for a in self.collection.get("artist_counts", {}):
            if a:
                d.setdefault(self.canon_artist_key(a), self.canon_artist_name(a))
        for ck, v in self.graph_rescore()["artists"].items():
            d.setdefault(ck, v.get("name", ck))
        return d

    def djset_rows(self):
        """Lignes de corpus djset, notées (score album) et regroupées par DJ puis vidéo."""
        ridx = self.reco_index
        by_dj = {}
        for r in self.corpus:
            if r.get("source") != "djset":
                continue
            sc, _ = self.album_score({
                "label": [r["label"]] if r.get("label") else [],
                "title": f"{r.get('artist', '')} - {r.get('title', '')}",
                "style": r.get("style") or []})
            row = dict(r, _score=sc)
            by_dj.setdefault(r.get("dj", "?"), {}).setdefault(r.get("video", "?"), []).append(row)
        return by_dj

    def graph_rescore(self):
        """{'artists': {ck: {name,id,score,why}}, 'labels': {lk: {...}}} — proximité
        recalculée à partir des arêtes brutes + rangs courants (porté de crate_radar)."""
        if self._graph_rs is not None:
            return self._graph_rs
        g = self.graph or {}
        gp = self.scoring["graph"]
        tw = gp["tier_w"]
        TW = {"1": tw["1"], "2": tw["2"], "3": tw["3"], None: tw["none"]}
        abr, lbr, c1b = gp["artist_breadth"], gp["label_breadth"], gp["cat1_bonus"]
        tiers = self.artist_tier_map()
        edges = g.get("edges")
        if not edges:
            self._graph_rs = {"artists": {}, "labels": g.get("labels", {})}
            return self._graph_rs
        seeds = g.get("seeds", {})
        arts = {}
        for ck, e in edges.items():
            if ck in tiers:
                continue
            base, cat1, byseed = 0.0, 0, {}
            for sk, d in e.get("co", {}).items():
                t = tiers.get(sk)
                base += d["n"] * d.get("rw", 1.0) * TW.get(t, 0.3)
                if t == "1":
                    cat1 += d["n"]
                byseed[seeds.get(sk, sk)] = d["n"]
            breadth = len(e.get("co", {}))
            score = round(base * (1 + abr * (breadth - 1)) + (c1b if cat1 else 0), 2)
            why = [f"{n}× avec {sn}" for sn, n in sorted(byseed.items(), key=lambda kv: -kv[1])[:3]]
            if cat1:
                why.insert(0, f"⭐ {cat1}× avec un artiste Cœur")
            arts[ck] = {"name": e["name"], "id": e.get("id"), "score": score, "why": why}
        watch = {normalize_label(x) for x in self.cfg.get("watchlist", [])}
        basek = {normalize_label(x) for x in self.cfg.get("labels", [])}
        labs = {}
        for lk, le in g.get("label_edges", {}).items():
            co = le.get("co", {})
            if not co:
                continue
            b = sum(n * TW.get(tiers.get(sk), 0.3) for sk, n in co.items())
            c1s = sum(1 for sk in co if tiers.get(sk) == "1")
            n_seeds = len(co)
            labs[lk] = {"name": le["name"],
                        "score": round(b * (1 + lbr * (n_seeds - 1)) + (c1b if c1s else 0), 2),
                        "n_seeds": n_seeds, "cat1_seeds": c1s,
                        "seeds": [seeds.get(sk, sk) for sk in list(co)[:6]],
                        "in_watchlist": lk in watch, "in_base": lk in basek}
        self._graph_rs = {
            "artists": dict(sorted(arts.items(), key=lambda kv: -kv[1]["score"])),
            "labels": dict(sorted(labs.items(), key=lambda kv: -kv[1]["score"])),
        }
        return self._graph_rs

    @property
    def ascore(self):
        """{clé canonique: score 0-100} — manuel + corpus + collection + proximité graphe."""
        if self._ascore is None:
            tiers = self.artist_tier_map()
            aw = self.scoring["artist_tiers"]
            sw = self.scoring["artist_score"]
            corpus_c, coll_c = {}, {}
            for r in self.corpus:
                a = (r.get("artist") or "").strip()
                if not a or normalize_label(a) in ARTIST_STOPWORDS:
                    continue
                corpus_c[self.canon_artist_key(a)] = corpus_c.get(self.canon_artist_key(a), 0) + 1
            for a, n in self.collection.get("artist_counts", {}).items():
                if not a or normalize_label(a) in ARTIST_STOPWORDS:
                    continue
                coll_c[self.canon_artist_key(a)] = coll_c.get(self.canon_artist_key(a), 0) + n
            graph = {ck: v["score"] for ck, v in self.graph_rescore()["artists"].items()}
            mc = max(corpus_c.values(), default=1)
            ml = max(coll_c.values(), default=1)
            mg = max(graph.values(), default=1) or 1
            out = {}
            for k in set(tiers) | set(corpus_c) | set(coll_c) | set(graph):
                tier = tiers.get(k)
                manual = float(aw.get(tier, 0)) if tier else 0.0
                out[k] = min(100, round(100 * (sw.get("manual", 0.5) * manual
                                      + sw.get("corpus", 0.18) * corpus_c.get(k, 0) / mc
                                      + sw.get("collection", 0.1) * coll_c.get(k, 0) / ml
                                      + sw.get("graph", 0.14) * graph.get(k, 0) / mg)))
            self._ascore = out
        return self._ascore

    def corpus_by_source(self):
        out = {}
        for r in self.corpus:
            out[r.get("source", "?")] = out.get(r.get("source", "?"), 0) + 1
        return out

    def stats(self):
        ac = self.cfg.get("artist_categories", {})
        res_ok = sum(1 for v in self.artists_res.values()
                     if v.get("discogs_id") and v.get("status") in ("exact", "approx", "confirmed"))
        return {
            "labels": len(self.cfg.get("labels", [])),
            "labels_profiled": len(self.profile),
            "coeur": len(ac.get("1", [])),
            "aimes": len(ac.get("2", [])),
            "artists_resolved": res_ok,
            "artists_identified": len(self.ascore),
            "graph_edges": len((self.graph or {}).get("edges", {})),
            "tracks": len(self.corpus),
            "tracks_by_source": self.corpus_by_source(),
            "watchlist": len(self.cfg.get("watchlist", [])),
            "sellers": len(self.cfg.get("sellers", [])),
        }

    def split_credit_artists(self, s):
        out = []
        for p in _CREDIT_SPLIT.split(s or ""):
            p = re.sub(r"\s*\*+\s*$", "", p.split("=")[0].strip())
            if p and normalize_label(p) not in ARTIST_STOPWORDS and p not in out:
                out.append(p)
        return out

    # -------------------------------------------------------------- labels / reco
    def corpus_label_scores(self):
        srcw = self.scoring["sources"]
        agg = {}
        for r in self.corpus:
            lab = r.get("label")
            if not lab:
                continue
            k = normalize_label(lab)
            agg[k] = agg.get(k, 0.0) + srcw.get(r.get("source"), 0.4)
        return agg

    def label_artist_signal(self):
        """{label_norm: (somme des scores d'artiste / 100, nb)} — via le corpus (v0)."""
        asc = self.ascore
        out = {}
        for r in self.corpus:
            lab, art = r.get("label"), r.get("artist")
            if not lab or not art:
                continue
            k = normalize_label(lab)
            s = asc.get(self.canon_artist_key(art), 0) / 100.0
            cur = out.get(k, (0.0, 0))
            out[k] = (cur[0] + s, cur[1] + 1)
        return out

    @property
    def reco_index(self):
        if self._reco_idx is None:
            self._reco_idx = {r["key"]: r["score"] for r in self.reco_rows()}
        return self._reco_idx

    def reco_rows(self):
        lc = self.collection.get("label_counts", {})
        wc = self.collection.get("want_label_counts", {})
        cs = self.corpus_label_scores()
        las = self.label_artist_signal()
        w = self.scoring["reco"]
        wf = float(w.get("want_factor", 0.6))
        w_coll, w_aff = float(w.get("collection", 0.6)), float(w.get("affinity", 0.4))
        w_corp, w_art = float(w.get("corpus", 0.5)), float(w.get("artist", 0.4))
        coll_raw = {k: lc.get(k, 0) + wf * wc.get(k, 0) for k in set(lc) | set(wc)}
        max_coll = max(coll_raw.values(), default=1.0) or 1.0
        max_corp = max(cs.values(), default=1.0) or 1.0
        max_art = max((v[0] for v in las.values()), default=1.0) or 1.0
        floor = float(self.scoring["label_affinity_floor"] or 0)
        watch = {normalize_label(x) for x in self.cfg.get("watchlist", [])}
        base = {normalize_label(x) for x in self.cfg.get("labels", [])}
        rows = []
        for k in set(coll_raw) | set(cs) | set(las):
            info = self.collection.get("label_ids", {}).get(k, {})
            e = self.profile.get(k)
            aff = self.affinity_score(e) if e else None
            if floor and (aff is None or aff < floor):
                continue
            art_val, art_n = las.get(k, (0.0, 0))
            feat = {"collection": coll_raw.get(k, 0) / max_coll,
                    "corpus": cs.get(k, 0) / max_corp,
                    "artist": art_val / max_art,
                    "affinity": (aff / 100) if aff is not None else 0}
            score = min(100, round(100 * (w_coll * feat["collection"] + w_corp * feat["corpus"]
                                 + w_art * feat["artist"] + w_aff * feat["affinity"])))
            rows.append({"key": k, "name": info.get("name") or k, "score": score,
                         "owned": lc.get(k, 0), "want": wc.get(k, 0),
                         "corpus": round(cs.get(k, 0), 1), "aff": aff, "artists": art_n,
                         "watched": k in watch, "in_base": k in base, "feat": feat})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    # -------------------------------------------------------------- score album
    def album_score(self, r):
        w = self.scoring["album"]
        lscore = None
        for lb in (list(r.get("label", [])) + ([r["_base_label"]] if r.get("_base_label") else [])):
            v = self.reco_index.get(normalize_label(lb))
            if v is not None:
                lscore = max(lscore or 0, v)
        a_str, sep, _ = (r.get("title") or "").partition(" - ")
        arts = self.split_credit_artists(a_str) if sep else []
        ascores = [self.ascore.get(self.canon_artist_key(a), 0) for a in arts]
        bl = float(w.get("artist_max_vs_mean", 0.6))
        a_term = (round(bl * max(ascores) + (1 - bl) * (sum(ascores) / len(ascores)))
                  if ascores else None)
        s_term = self.style_affinity_of(r.get("style"))
        terms = []
        if lscore is not None:
            terms.append((float(w.get("label", 0.4)), lscore))
        if a_term is not None:
            terms.append((float(w.get("artist", 0.4)), a_term))
        if s_term is not None:
            terms.append((float(w.get("style", 0.2)), s_term))
        tot = sum(t[0] for t in terms)
        if not tot:
            return None, {}
        return (min(100, round(sum(t[0] * t[1] for t in terms) / tot)),
                {"label": lscore, "artist": a_term, "style": s_term})


def real_tracks(tracklist):
    out = []
    for t in tracklist or []:
        if t.get("type_", "track") != "track":
            continue
        title = (t.get("title") or "").strip()
        if not title or _SIDE_MARKER.match(title):
            continue
        out.append(t)
    return out


def yt_search_url(query):
    import urllib.parse
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus((query or "").strip())
