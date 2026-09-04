"""Chaîne de notation — portée de crate_radar.py, sans dépendance Streamlit.
Un objet Ctx charge toutes les données une fois ; les fonctions le prennent en argument.

v0 : label + style complets ; terme « artiste » simplifié (liste manuelle + corpus +
collection ; le graphe de producteurs viendra ensuite)."""
import math
import os
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


def _robust_scale(counts):
    """p95 des valeurs (jamais le max) : un seul artiste hyperactif ne doit
    pas à lui seul déplacer l'échelle de tous les autres (cf. diagnostic N5).
    Toujours >= 1 (jamais de division par zéro en aval)."""
    vals = sorted(counts.values())
    if not vals:
        return 1
    return vals[int(0.95 * (len(vals) - 1))] or 1


def _log_ratio(n, scale):
    """log1p(n)/log1p(scale), plafonné à 1 — écrase les valeurs extrêmes au
    lieu d'un simple n/max linéaire (cf. diagnostic N5)."""
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(scale))

# Ctx est reconstruit à chaque requête, mais les fichiers ne changent qu'au passage
# d'un job. Les calculs dérivés coûteux (graphe, scores) sont donc mémorisés sous la
# signature (mtime, taille) de leurs fichiers d'entrée : toute écriture d'un job ou de
# save_config change la signature et invalide d'office l'entrée précédente.
_DERIVED = {}
_DERIVED_MAX = 8


def _files_sig(*paths_):
    out = []
    for p in paths_:
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class Ctx:
    """Instantané des données. Recréer à chaque requête (peu coûteux, fichiers < 3 Mo)."""

    def __init__(self, uid=None):
        self.uid = uid or store.current_uid()
        self.P = paths.user_paths(self.uid)
        self.cfg = store.read_config(self.uid)
        self.scoring = self.cfg["scoring"]
        # gros fichiers relus à chaque requête -> cache invalidé sur mtime
        self.profile = store.load_cached(self.P.profile, {})
        self.collection = store.load_cached(self.P.collection, {})
        self.corpus = store.load_cached(self.P.corpus, [])
        self.resolved = store.load_cached(self.P.resolved, {})
        self.artists_res = store.load_cached(self.P.artists_res, {})
        self.graph = store.load_cached(self.P.graph, {})
        from . import discogs_dump as dd
        self._key = (self.uid, _files_sig(
            self.P.config, self.P.graph, self.P.artists_res,
            self.P.corpus, self.P.collection, self.P.profile, dd.DB_PATH))

    def _memo(self, name, compute):
        slot = _DERIVED.get(self._key)
        if slot is None:
            if len(_DERIVED) >= _DERIVED_MAX:
                _DERIVED.clear()          # purge simple : la signature courante sera
                                          # recalculée au prochain appel
            slot = _DERIVED[self._key] = {}
        if name not in slot:
            slot[name] = compute()
        return slot[name]

    # -------------------------------------------------------------- goût / styles
    @property
    def wmap(self):
        return self._memo("wmap", self._compute_wmap)

    def _compute_wmap(self):
        cats = self.cfg.get("taste_categories", store.DEFAULT_TASTE_CATEGORIES)
        w = self.scoring["taste_tiers"]
        m = {}
        for cid, styles in cats.items():
            wc = float(w.get(cid, 0))
            for s in styles:
                m[style_key(s)] = wc
        return m

    def affinity_score(self, entry):
        """(affinité 0-100 ou None, couverture 0-100). Un style absent de
        `wmap` (hors de mes catégories de goût) est exclu du calcul plutôt
        que compté comme 0 — sinon « la moitié du catalogue est dans un
        style que je n'ai pas classé » se confond avec « la moitié est dans
        un style que je déteste explicitement » (cf. diagnostic N4). La part
        exclue du calcul redevient visible séparément dans `coverage`."""
        sc = (entry or {}).get("style_counts") or {}
        total = sum(sc.values())
        if not total:
            return None, 0
        known_total = sum(n for s, n in sc.items() if style_key(s) in self.wmap)
        coverage = round(100 * known_total / total)
        if not known_total:
            return None, coverage
        weighted = sum(n * self.wmap.get(style_key(s), 0.0)
                       for s, n in sc.items() if style_key(s) in self.wmap)
        return min(100, round(100 * weighted / known_total)), coverage

    def label_affinities(self, label_keys):
        """{label_key: {'aff': 0-100 ou None, 'coverage': 0-100}} — priorité
        au profil matérialisé depuis le dump Discogs (table label_styles,
        exhaustif sur tout le catalogue vinyle importé), repli sur
        labels_profile.json (échantillon API biaisé par le tri "want", cf.
        diagnostic D5) pour les labels absents du dump. aff=None et
        coverage=0 si aucune des deux sources n'a de données (jamais
        profilé) — à distinguer d'un aff bas mais réel."""
        from . import discogs_dump as dd
        keys = list(label_keys)
        dump_styles = dd.label_style_counts(keys)
        out = {}
        for k in keys:
            dstyles = dump_styles.get(k)
            entry = {"style_counts": dstyles} if dstyles else self.profile.get(k)
            aff, cov = self.affinity_score(entry) if entry else (None, 0)
            out[k] = {"aff": aff, "coverage": cov}
        return out

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

    def seed_category_weight(self):
        """{clé canonique: poids} pour pondérer une graine du graphe selon sa
        provenance (2026-09-04) : Cœur/Aimés (poids scoring.graph.tier_w,
        prioritaire), sinon la meilleure source où l'artiste apparaît dans le
        corpus (poids scoring.sources — mêmes poids que pour le score label :
        Bandcamp/collection Discogs plus engagés qu'une écoute YouTube
        passive, DJ sets encore en-dessous), sinon le poids plancher
        (tier_w.none) pour n'importe quel autre artiste devenu graine (mode
        global). Mémoïsé comme le reste : ne dépend que de la config et du
        corpus, jamais du graphe lui-même (pas de dépendance circulaire)."""
        return self._memo("seed_category_weight", self._compute_seed_category_weight)

    def _compute_seed_category_weight(self):
        tw = self.scoring["graph"]["tier_w"]
        srcw = self.scoring["sources"]
        out = {k: float(tw.get(cid, tw["none"])) for k, cid in self.artist_tier_map().items()}
        for r in self.corpus:
            a = (r.get("artist") or "").strip()
            if not a or normalize_label(a) in ARTIST_STOPWORDS:
                continue
            k = self.canon_artist_key(a)
            if k in out:                # Cœur/Aimés déjà prioritaires, jamais rétrogradés
                continue
            w = float(srcw.get(r.get("source"), tw["none"]))
            out[k] = max(out.get(k, 0.0), w)
        return out

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
        return self._memo("graph_rs", self._compute_graph_rescore)

    def _compute_graph_rescore(self):
        g = self.graph or {}
        gp = self.scoring["graph"]
        abr, lbr, c1b = gp["artist_breadth"], gp["label_breadth"], gp["cat1_bonus"]
        tiers = self.artist_tier_map()
        seedw = self.seed_category_weight()
        edges = g.get("edges")
        if not edges:
            return {"artists": {}, "labels": g.get("labels", {})}
        seeds = g.get("seeds", {})
        arts = {}
        for ck, e in edges.items():
            if ck in tiers:
                continue
            base, cat1, byseed = 0.0, 0, {}
            for sk, d in e.get("co", {}).items():
                t = tiers.get(sk)
                base += d["n"] * d.get("rw", 1.0) * seedw.get(sk, gp["tier_w"]["none"])
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
            b = sum(n * seedw.get(sk, gp["tier_w"]["none"]) for sk, n in co.items())
            c1s = sum(1 for sk in co if tiers.get(sk) == "1")
            n_seeds = len(co)
            labs[lk] = {"name": le["name"],
                        "score": round(b * (1 + lbr * (n_seeds - 1)) + (c1b if c1s else 0), 2),
                        "n_seeds": n_seeds, "cat1_seeds": c1s,
                        "seeds": [seeds.get(sk, sk) for sk in list(co)[:6]],
                        "in_watchlist": lk in watch, "in_base": lk in basek}
        return {
            "artists": dict(sorted(arts.items(), key=lambda kv: -kv[1]["score"])),
            "labels": dict(sorted(labs.items(), key=lambda kv: -kv[1]["score"])),
        }

    @property
    def ascore(self):
        """{clé canonique: score 0-100} — manuel + corpus + collection + proximité graphe."""
        return self._memo("ascore", self._compute_ascore)

    def _compute_ascore(self):
        tiers = self.artist_tier_map()
        aw = self.scoring["artist_tiers"]
        sw = self.scoring["artist_score"]
        corpus_c, coll_c, djset_c = {}, {}, {}
        for r in self.corpus:
            a = (r.get("artist") or "").strip()
            if not a or normalize_label(a) in ARTIST_STOPWORDS:
                continue
            k = self.canon_artist_key(a)
            # djset compté à part (poids "djset" dédié, cf. N1) : sinon ces lignes
            # pèseraient deux fois, une fois ici et une fois dans le terme djset.
            if r.get("source") == "djset":
                djset_c[k] = djset_c.get(k, 0) + 1
            else:
                corpus_c[k] = corpus_c.get(k, 0) + 1
        for a, n in self.collection.get("artist_counts", {}).items():
            if not a or normalize_label(a) in ARTIST_STOPWORDS:
                continue
            coll_c[self.canon_artist_key(a)] = coll_c.get(self.canon_artist_key(a), 0) + n
        graph = {ck: v["score"] for ck, v in self.graph_rescore()["artists"].items()}
        # l'artiste a-t-il un disque chez un label que je suis déjà (base/watchlist) ?
        # référentiel local — une relation différente du terme "graph" ci-dessus
        # (co-crédits entre artistes), donc sans le recouper (cf. Ctx.artist_label_signal).
        label_link = self.artist_label_signal()
        scale_c, scale_l, scale_d = _robust_scale(corpus_c), _robust_scale(coll_c), _robust_scale(djset_c)
        # p95 + log1p plutôt qu'un simple ratio au max brut (cf. N5) : un seul artiste très
        # prolifique avec une graine Cœur (ex. un alias avec 30 sorties partagées) ne doit
        # pas comprimer le terme graphe de tous les autres en tirant le maximum vers le haut
        # (diagnostic soulevé le 2026-09-04 — même défaut que N5 avait déjà corrigé ailleurs).
        scale_g = _robust_scale(graph)
        w_manual, w_corpus = sw.get("manual", 0.5), sw.get("corpus", 0.18)
        w_coll, w_graph = sw.get("collection", 0.1), sw.get("graph", 0.14)
        w_djset, w_label = sw.get("djset", 0.08), sw.get("label_link", 0.15)
        # normalisé par la somme des poids réellement en jeu (même principe que reco_rows,
        # diagnostic N2) : sans ça, ajouter un terme fait dériver le plafond au-delà de 100
        # au lieu de rééquilibrer les poids relatifs des signaux existants.
        tot = w_manual + w_corpus + w_coll + w_graph + w_djset + w_label or 1.0
        out = {}
        for k in set(tiers) | set(corpus_c) | set(coll_c) | set(graph) | set(djset_c) | set(label_link):
            tier = tiers.get(k)
            manual = float(aw.get(tier, 0)) if tier else 0.0
            # échelle robuste au p95 + log1p plutôt qu'au max brut (cf. N5) : un seul
            # artiste hyperactif (un DJ set où il revient 40 fois) ne doit pas écraser
            # la note de tous les autres en déplaçant le maximum de la population.
            raw = (w_manual * manual
                   + w_corpus * _log_ratio(corpus_c.get(k, 0), scale_c)
                   + w_coll * _log_ratio(coll_c.get(k, 0), scale_l)
                   + w_graph * _log_ratio(graph.get(k, 0), scale_g)
                   + w_djset * _log_ratio(djset_c.get(k, 0), scale_d)
                   + w_label * label_link.get(k, 0.0))
            out[k] = min(100, round(100 * raw / tot))
        return out

    def corpus_by_source(self):
        out = {}
        for r in self.corpus:
            out[r.get("source", "?")] = out.get(r.get("source", "?"), 0) + 1
        return out

    def stats(self):
        ac = self.cfg.get("artist_categories", {})
        res_ok = sum(1 for v in self.artists_res.values()
                     if v.get("discogs_id") and v.get("status") in ("exact", "approx", "confirmed"))
        not_found = (sum(1 for v in self.resolved.values() if v.get("status") == "not_found")
                     + sum(1 for v in self.artists_res.values() if v.get("status") == "not_found"))
        return {
            "labels": len(self.cfg.get("labels", [])),
            "labels_profiled": len(self.profile),
            "coeur": len(ac.get("1", [])),
            "aimes": len(ac.get("2", [])),
            "artists_resolved": res_ok,
            "artists_identified": len(self.ascore),
            "not_found": not_found,
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

    def label_db_signal(self):
        """{label_key: 0-1} — deux calculs complémentaires du référentiel
        local (aucun appel API), tous deux "un artiste que j'aime a un
        disque chez ce label" mais avec des garanties différentes : DIRECT
        (`discogs_dump.label_ids_for_artists`, artistes Cœur/Aimés
        uniquement, disponible immédiatement, aucun job requis) et GRAPHE
        (`label_edges` déjà calculé par job_build_graph, pondéré par palier
        Cœur/Aimés/autre — plus large car il couvre aussi les artistes
        seulement résolus, mais seulement à jour depuis le dernier lancement
        du job). Combinés pour ne pas dépendre uniquement du job ; jamais
        redondants avec `label_artist_signal` (qui ne voit que le corpus
        écouté, un sous-ensemble minuscule du catalogue)."""
        return self._memo("label_db_signal", self._compute_label_db_signal)["score"]

    def label_db_names(self):
        """{label_key: nom} — pour les labels qui n'apparaissent QUE via
        label_db_signal (jamais possédés/écoutés, donc absents de
        collection.label_ids) : reco_rows a besoin d'un nom à afficher."""
        return self._memo("label_db_signal", self._compute_label_db_signal)["name"]

    def _compute_label_db_signal(self):
        from . import discogs_dump as dd
        liked_ids = [int(k[3:]) for k in self.artist_tier_map() if k.startswith("id:")]
        direct = dd.label_ids_for_artists(liked_ids) if liked_ids else {}
        direct_n = {k: len(v["artist_ids"]) for k, v in direct.items()}
        graph = self.graph_rescore()["labels"]
        graph_sc = {k: v["score"] for k, v in graph.items()}
        names = {k: v["name"] for k, v in graph.items()}
        names.update({k: v["name"] for k, v in direct.items()})   # direct prioritaire si les 2 existent
        scale_d, scale_g = _robust_scale(direct_n), _robust_scale(graph_sc)
        score = {}
        for k in set(direct_n) | set(graph_sc):
            d = _log_ratio(direct_n.get(k, 0), scale_d)
            g = _log_ratio(graph_sc.get(k, 0), scale_g)
            score[k] = round(0.65 * d + 0.35 * g, 4)   # direct plus fiable (Cœur/Aimés stricts,
            # pas d'attente de job) qu'un score de graphe agrégé sur toutes les graines résolues
        return {"score": score, "name": names}

    def artist_label_signal(self):
        """{clé canonique: 0-1} — l'artiste a-t-il un disque chez un label
        que je suis déjà (base ou watchlist) ? Mirroir, côté artistes, de la
        moitié "directe" de `label_db_signal` (`discogs_dump.
        artist_ids_for_labels`). Le co-crédit entre artistes (une relation
        différente : "a-t-il bossé avec quelqu'un que j'aime") alimente déjà
        `ascore` séparément via le terme `graph` — pas de recoupement."""
        return self._memo("artist_label_signal", self._compute_artist_label_signal)

    def _compute_artist_label_signal(self):
        from . import discogs_dump as dd
        liked_keys = ({normalize_label(x) for x in self.cfg.get("labels", [])}
                      | {normalize_label(x) for x in self.cfg.get("watchlist", [])})
        if not liked_keys:
            return {}
        hits = dd.artist_ids_for_labels(liked_keys)
        counts = {f"id:{aid}": v["n"] for aid, v in hits.items()}
        scale = _robust_scale(counts)
        return {k: _log_ratio(n, scale) for k, n in counts.items()}

    @property
    def reco_index(self):
        # reco_rows() est déjà mémoïsé, mais reconstruire ce dict à chaque accès a un
        # coût réel : album_score() y accède dans une boucle, sur ~50-100 lignes de
        # résultats par recherche -> autant de rebuilds d'un dict de la taille de la
        # base de labels (diagnostic Lot 1, mineur).
        return self._memo("reco_index", lambda: {r["key"]: r["score"] for r in self.reco_rows()})

    def reco_rows(self):
        return self._memo("reco_rows", self._compute_reco_rows)

    def _compute_reco_rows(self):
        lc = self.collection.get("label_counts", {})
        wc = self.collection.get("want_label_counts", {})
        cs = self.corpus_label_scores()
        las = self.label_artist_signal()
        db = self.label_db_signal()
        w = self.scoring["reco"]
        wf = float(w.get("want_factor", 0.6))
        w_coll, w_aff = float(w.get("collection", 0.6)), float(w.get("affinity", 0.4))
        w_corp, w_art = float(w.get("corpus", 0.5)), float(w.get("artist", 0.4))
        w_db = float(w.get("db_link", 0.35))
        coll_raw = {k: lc.get(k, 0) + wf * wc.get(k, 0) for k in set(lc) | set(wc)}
        max_coll = max(coll_raw.values(), default=1.0) or 1.0
        max_corp = max(cs.values(), default=1.0) or 1.0
        max_art = max((v[0] for v in las.values()), default=1.0) or 1.0
        floor = float(self.scoring["label_affinity_floor"] or 0)
        watch = {normalize_label(x) for x in self.cfg.get("watchlist", [])}
        base = {normalize_label(x) for x in self.cfg.get("labels", [])}
        # `db` (artiste Cœur/Aimés au catalogue + graphe de co-crédits, référentiel local)
        # élargit l'univers classé au-delà de ce que collection/corpus/las connaissent déjà —
        # un label jamais possédé ni écouté mais où un artiste aimé a un disque doit pouvoir
        # apparaître, pas seulement être visible dans la liste "candidats du graphe" à part.
        keys = set(coll_raw) | set(cs) | set(las) | set(db)
        affinities = self.label_affinities(keys)
        db_names = self.label_db_names()
        rows = []
        for k in keys:
            info = self.collection.get("label_ids", {}).get(k, {})
            aff, coverage = affinities[k]["aff"], affinities[k]["coverage"]
            # le seuil filtre sur une affinité RÉELLEMENT basse, jamais sur "pas encore
            # profilé" (aff is None) — sinon on masque silencieusement tout label non
            # traité par un job, en faisant croire qu'on a filtré sur la qualité (N4).
            if floor and aff is not None and aff < floor:
                continue
            art_val, art_n = las.get(k, (0.0, 0))
            feat = {"collection": coll_raw.get(k, 0) / max_coll,
                    "corpus": cs.get(k, 0) / max_corp,
                    "artist": art_val / max_art,
                    "affinity": (aff / 100) if aff is not None else 0,
                    "db_link": db.get(k, 0.0)}
            # normalisé par la somme des poids réellement en jeu (jamais Σw fixe = 1.9,
            # cf. diagnostic N2) : la même formule que album_score, sur la même échelle /100.
            terms = [(w_coll, feat["collection"]), (w_corp, feat["corpus"]),
                     (w_art, feat["artist"]), (w_aff, feat["affinity"]),
                     (w_db, feat["db_link"])]
            tot = sum(t[0] for t in terms)
            score = min(100, round(100 * sum(t[0] * t[1] for t in terms) / tot)) if tot else 0
            rows.append({"key": k, "name": info.get("name") or db_names.get(k) or k, "score": score,
                         "owned": lc.get(k, 0), "want": wc.get(k, 0),
                         "corpus": round(cs.get(k, 0), 1), "aff": aff, "coverage": coverage,
                         "artists": art_n, "watched": k in watch, "in_base": k in base, "feat": feat})
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
        # rétrécissement vers un a priori neutre (cf. diagnostic N3) : un score
        # fondé sur un seul critère disponible s'affichait à 100 % de confiance
        # avec le même badge qu'un score fondé sur les trois — alors qu'on en sait
        # objectivement moins. K/PRIOR tirent le résultat vers un centre neutre
        # d'autant plus fort que peu de poids est réellement en jeu (tot petit).
        K, PRIOR = 0.35, 45
        score = min(100, round((sum(t[0] * t[1] for t in terms) + K * PRIOR) / (tot + K)))
        return score, {"label": lscore, "artist": a_term, "style": s_term}


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
