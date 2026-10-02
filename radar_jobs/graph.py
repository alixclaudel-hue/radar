"""Graphe producteurs/artistes (étape 4b) : `build_graph`, `resolve_artists`.
"""

import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime

from radar_jobs.common import (
    ARTIST_STOPWORDS, ARTISTS_RESOLVED_PATH, cfg_load, COLLECTION_CACHE_PATH, CORPUS_PATH,
    load_json, normalize_label, PRODUCER_GRAPH_PATH, save_json,
)
from radar_jobs.sources import discogs_get, discogs_search


# ---- build_graph (étape 4b) -------------------------------------------------

_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&| feat\.? | ft\.? | vs\.? | and | x | with | pres\.? )\s*", re.I)
_LABEL_STOP = {"not on label", "self-released", "self released", "white label", "no label",
               "none", "unknown", "resident advisor", "n/a", "promo only", "dmc",
               "tm century", "cd pool", "ministry of sound", "universal", "columbia",
               "emi", "sony music", "warner music", "polystar", "zyx music",
               "wagram music", "more music and media"}
_LABEL_STOP_SUB = ["magazine", "podcast", "mixtape", "not on label", "self-released",
                   "promo only", "dj center", "bootleg"]


def _clean_graph_label(name):
    """Nom de label propre pour le graphe, ou "" si à écarter."""
    s = (name or "").split(",")[0].strip()                 # "GR Records, GR Records" -> "GR Records"
    s = re.sub(r"\s*\(\d+\)\s*$", "", s).strip()           # "(2)" désambiguïsation
    low = s.lower()
    if len(s) < 2 or low in _LABEL_STOP or any(x in low for x in _LABEL_STOP_SUB):
        return ""
    return s


def _split_artists(s):
    """Découpe une chaîne de crédit Discogs en noms individuels, tels que Discogs les
    écrit (on garde « (2) » qui désambiguïse un homonyme ; on retire juste le « * »
    de variation)."""
    out = []
    for p in _ARTIST_SPLIT.split(s or ""):
        p = p.split("=")[0]                        # "A = 寺田創一" -> "A"
        p = re.sub(r"\s*\*+\s*$", "", p.strip())   # marqueur de variation Discogs
        if p and normalize_label(p) not in ARTIST_STOPWORDS and p not in out:
            out.append(p)
    return out


def _seed_artists(cfg):
    """Retourne (scored, tier, disp) :
    scored = [(clé, nom, score)] classé du plus fort au plus faible,
    tier = {clé: '1'|'2'|'3'}, disp = {clé: nom d'affichage}."""
    cats = cfg.get("artist_categories", {"1": [], "2": [], "3": []})
    aw = cfg.get("artist_weights", {"1": 1.0, "2": 0.6, "3": 0.3})
    sw = cfg.get("artist_score_weights", {"manual": 0.6, "corpus": 0.25, "collection": 0.15})
    corpus = load_json(CORPUS_PATH, [])
    coll = load_json(COLLECTION_CACHE_PATH, {})
    cc, lc, disp = Counter(), Counter(), {}
    for r in corpus:
        a = (r.get("artist") or "").strip()
        k = normalize_label(a)
        if k and k not in ARTIST_STOPWORDS:
            cc[k] += 1
            disp.setdefault(k, a)
    for a, n in coll.get("artist_counts", {}).items():
        k = normalize_label(a)
        if k and k not in ARTIST_STOPWORDS:
            lc[k] += n
            disp.setdefault(k, a)
    tier = {}
    for cid, names in cats.items():
        for n in names:
            tier[normalize_label(n)] = cid
            disp.setdefault(normalize_label(n), n)
    mc = max(cc.values(), default=1)
    ml = max(lc.values(), default=1)
    out = {}
    for k in set(tier) | set(cc) | set(lc):
        s = (sw.get("manual", .6) * (float(aw.get(tier.get(k), 0)))
             + sw.get("corpus", .25) * (cc.get(k, 0) / mc)
             + sw.get("collection", .15) * (lc.get(k, 0) / ml))
        out[k] = (s, disp.get(k, k))
    ranked = sorted(out.items(), key=lambda kv: kv[1][0], reverse=True)
    scored = [(k, v[1], v[0]) for k, v in ranked]
    return scored, tier, disp


def _canon_key(name, ares):
    e = ares.get(normalize_label(name))
    if e and e.get("discogs_id") and e.get("status") in ("exact", "approx", "confirmed"):
        return f"id:{e['discogs_id']}"
    return normalize_label(name)


VARIOUS_ARTIST_ID = 194  # id Discogs de l'artiste générique "Various" — marqueur de facto
                          # des compilations, qui ne portent qu'1-3 <artists> release-level
                          # (le vrai détail est au niveau piste, non parsé, cf. diagnostic R2)


def _sql_in_chunks(con, sql_tmpl, ids, extra_params=()):
    """Exécute sql_tmpl (un seul '{}' à la place du IN (...)) par lots de 900
    ids — reste sous SQLITE_MAX_VARIABLE_NUMBER même sur un build restrictif
    (défaut historique 999) au lieu de laisser un artiste à discographie
    énorme (ou un id mal fusionné) lever OperationalError et tuer tout le
    job de graphe (diagnostic Lot 5, garde-fou manquant)."""
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        qmarks = ",".join("?" * len(chunk))
        out += con.execute(sql_tmpl.format(qmarks), chunk + list(extra_params)).fetchall()
    return out


def _expand_node(con, rid, sk, weight, seed_set, max_credits, rw_by_role,
                  art_edges, lab_edges, visited, next_frontier, node_cap):
    """Co-crédits directs de `rid` (une sortie de la BFS bornée, cf.
    `_graph_edges_from_sql`), pondérés par `weight` (poids de rôle × décroissance
    du niveau courant). `visited`/`next_frontier` sont mutés en place : un nœud
    déjà visité à un niveau <= celui-ci (donc déjà crédité, à un poids au moins
    aussi fort) n'est jamais recompté ni ré-exploré — la comparaison se fait sur
    un instantané pris AVANT ce nœud, pour que plusieurs sorties partagées avec
    CE `rid` continuent de s'additionner normalement dans le même appel. Retourne
    le nombre de sorties retenues (hors compilations). 0 (pas une erreur) si la
    base n'a pas encore été reconstruite au nouveau schéma (release_artists créée
    au Lot 4) — même garde que label_style_counts plutôt qu'un job qui plante."""
    already = frozenset(visited)
    try:
        rows = con.execute("""
            SELECT ra1.release_id, ra1.role, ra2.artist_id, a.name, r.label, r.label_key
            FROM release_artists ra1
            JOIN release_artists ra2 ON ra2.release_id = ra1.release_id AND ra2.artist_id != ra1.artist_id
            JOIN releases r ON r.id = ra1.release_id
            LEFT JOIN artists a ON a.id = ra2.artist_id
            WHERE ra1.artist_id = ?
        """, (rid,)).fetchall()
        if not rows:
            return 0
        by_release = {}
        for rel_id, role, co_id, co_name, lab, lk in rows:
            info = by_release.setdefault(rel_id, {"role": role, "label": lab, "label_key": lk, "co": []})
            info["co"].append((co_id, co_name))
        rel_ids = list(by_release)
        main_counts = dict(_sql_in_chunks(con,
            "SELECT release_id, COUNT(*) FROM release_artists WHERE release_id IN ({}) AND role = 'Main' "
            "GROUP BY release_id", rel_ids))
        # une compilation "Various" ne porte souvent QU'1 <artists> release-level (l'artiste
        # générique "Various", id 194) : main_counts reste sous max_credits, le garde-fou par
        # comptage seul la laisse passer (diagnostic R2) -> détection dédiée par id.
        various_ids = {row[0] for row in _sql_in_chunks(con,
            "SELECT release_id FROM release_artists WHERE release_id IN ({}) AND role = 'Main' "
            "AND artist_id = ?", rel_ids, (VARIOUS_ARTIST_ID,))}
    except sqlite3.OperationalError:
        return 0
    n_rels = 0
    for rel_id, info in by_release.items():
        if rel_id in various_ids or main_counts.get(rel_id, 0) > max_credits:  # compilation -> tout sauté
            continue
        # une sortie qui ne relie qu'à des nœuds déjà visités (typiquement : le nœud en
        # cours d'expansion partage aussi une sortie AVEC LA GRAINE, ou avec un nœud
        # d'un niveau antérieur) ne représente aucune preuve nouvelle à ce niveau — déjà
        # comptée là où elle a été découverte pour la première fois. Sans ce filtre, une
        # même sortie serait recréditée (artistes ET labels) à chaque niveau qui la
        # retraverse, avec un poids différent à chaque fois.
        new_co = [(co_id, co_name) for co_id, co_name in info["co"] if co_id not in already]
        if not new_co:
            continue
        n_rels += 1
        rw = rw_by_role(info["role"]) * weight
        for co_id, co_name in new_co:
            ck = f"id:{co_id}"
            if ck not in seed_set and not (co_name and normalize_label(co_name) in ARTIST_STOPWORDS):
                ce = art_edges.setdefault(ck, {"name": co_name or ck, "id": co_id, "co": {}})
                slot = ce["co"].setdefault(sk, {"n": 0, "rw": 0.0})
                slot["n"] += 1
                slot["rw"] = max(slot["rw"], rw)
            if co_id not in visited and len(visited) < node_cap:
                visited.add(co_id)
                next_frontier.add(co_id)
        lab = _clean_graph_label(info["label"])
        if lab:
            lk = info["label_key"] or normalize_label(lab)
            le = lab_edges.setdefault(lk, {"name": lab, "co": {}})
            le["co"][sk] = le["co"].get(sk, 0.0) + weight
    return n_rels


def _graph_edges_from_sql(con, rid, sk, seed_set, max_credits, rw_by_role, art_edges, lab_edges,
                           max_levels=4, level_decay=0.5, node_cap=150):
    """BFS bornée depuis la graine `rid`, jusqu'à `max_levels` sauts de
    co-crédits (niveau 1 = co-crédit direct, poids plein ; chaque niveau
    suivant est multiplié par `level_decay`). `node_cap` plafonne le nombre
    de nœuds explorés PAR GRAINE : un graphe de co-crédits est un "petit
    monde" — sans plafond, 4 sauts depuis des centaines de graines (mode
    global) toucherait vite l'essentiel du catalogue et ferait perdre tout
    son sens de "proche de mes goûts" à la reco (en plus d'exploser le temps
    du job). Retourne le nombre total de sorties retenues, tous niveaux
    confondus."""
    visited, frontier = {rid}, {rid}
    n_rels_total = 0
    level = 1
    while frontier and level <= max_levels and len(visited) < node_cap:
        weight = level_decay ** (level - 1)
        next_frontier = set()
        for node in frontier:
            n_rels_total += _expand_node(con, node, sk, weight, seed_set, max_credits, rw_by_role,
                                          art_edges, lab_edges, visited, next_frontier, node_cap)
        frontier = next_frontier
        level += 1
    return n_rels_total


def _graph_edges_from_api(token, rid, sk, seed_set, ares, pages, max_credits, rw_by_role,
                           art_edges, lab_edges):
    """Repli API (référentiel local pas encore importé) — comportement historique :
    une à deux pages de /artists/{rid}/releases, pause entre chaque page."""
    rels = []
    for pg in range(1, pages + 1):
        d = discogs_get(token, f"/artists/{rid}/releases",
                        {"per_page": 100, "page": pg, "sort": "year", "sort_order": "desc"})
        rels += d.get("releases", [])
        if pg >= d.get("pagination", {}).get("pages", 1):
            break
        time.sleep(1.1)
    for rel in rels:
        co_names = _split_artists(rel.get("artist", ""))
        if len(co_names) > max_credits:        # compilation -> on saute tout (artistes + label)
            continue
        rw = rw_by_role(rel.get("role") or "Main")
        for co in co_names:
            ck = _canon_key(co, ares)
            if not ck or ck in seed_set or ck in ARTIST_STOPWORDS:
                continue
            ce = art_edges.setdefault(ck, {"name": co, "id": None, "co": {}})
            slot = ce["co"].setdefault(sk, {"n": 0, "rw": rw})
            slot["n"] += 1
            slot["rw"] = max(slot["rw"], rw)
            re_ = ares.get(normalize_label(co))
            if re_ and re_.get("discogs_id"):
                ce["id"] = re_["discogs_id"]
                ce["name"] = re_.get("discogs_name") or co
        lab = _clean_graph_label(rel.get("label"))
        if lab:
            lk = normalize_label(lab)
            le = lab_edges.setdefault(lk, {"name": lab, "co": {}})
            le["co"][sk] = le["co"].get(sk, 0) + 1
    return len(rels)


def job_build_graph(job, params):
    """Construit le graphe de producteurs et stocke les ARÊTES BRUTES (comptes de
    co-crédits par graine, poids de rôle, labels). Le score final est recalculé
    côté appli à partir des catégories courantes (pas besoin de re-fetch).

    Co-crédits par jointure SQL sur `release_artists` (radar/discogs_dump.py,
    D4) quand le référentiel local est disponible — aucun appel API, aucune
    pause, et un vrai dépouillement de compilation (HAVING sur un décompte
    de crédits `Main`, plutôt qu'un split de chaîne). Repli sur l'API
    (comportement historique, avec pause, 1 seul saut) si le dump n'est pas
    encore importé — cf. `_graph_edges_from_api`.

    mode="taste" (2026-09-04) : graines = Cœur + Aimés + tous les artistes du
    corpus (toutes sources, DJ sets compris — le poids par source, appliqué
    au moment du score plutôt qu'à la construction, vit dans
    Ctx.seed_category_weight). Traversée multi-niveaux (cf.
    `_graph_edges_from_sql` : max_levels/level_decay/node_cap)."""
    from radar_web.radar import discogs_dump as dd

    cfg = cfg_load()
    token = cfg.get("token", "")
    pages = int(params.get("pages", 2))
    mode = params.get("mode", "top")
    incremental = params.get("incremental")
    if incremental is None:
        # Reprise par défaut : le graphe est une image du dump, pas un calcul à refaire.
        # S'il existe déjà (complet OU partiel après un arrêt), on ne traite que les
        # graines qui manquent — sans quoi un timeout ou un redéploiement relançait
        # 6 h de calcul depuis zéro à chaque fois (incident du 29/09). `full=1` force
        # une reconstruction complète.
        incremental = (mode == "taste" and not params.get("full")
                       and os.path.exists(PRODUCER_GRAPH_PATH))
    incremental = bool(incremental)
    gsc =(cfg.get("scoring") or {}).get("graph", {})   # cf. DEFAULT_SCORING["graph"] côté appli
    max_credits = int(params.get("max_credits", gsc.get("max_credits", 6)))  # > N = compilation
    role_main = float(gsc.get("role_main", 1.0))
    role_remix = float(gsc.get("role_remix", 0.7))
    role_other = float(gsc.get("role_other", 0.4))
    role_written = float(gsc.get("role_written", 0.05))
    max_levels = int(gsc.get("max_levels", 4))
    level_decay = float(gsc.get("level_decay", 0.5))
    node_cap = int(gsc.get("node_cap", 150))

    def rw_by_role(role):
        # Written-By (auteur/compositeur) traverse tous les genres via les reprises —
        # un standard repris en cover/sample dans un style totalement différent crédite
        # toujours son auteur d'origine, contrairement à Producer/Remix/Main qui impliquent
        # une vraie collaboration artistique sur CETTE sortie. Sans un poids quasi nul,
        # un auteur prolifique (ex. Lennon-McCartney) devient un hub artificiel du graphe,
        # co-crédité des milliers de fois via des reprises sans aucun rapport de style
        # (cf. retour utilisateur 2026-09-05 : Lennon-McCartney devant Frankie Knuckles).
        if role == "Main":
            return role_main
        if role in ("Remix", "Producer"):
            return role_remix
        if role == "Written-By":
            return role_written
        return role_other

    ares = load_json(ARTISTS_RESOLVED_PATH, {})

    prev = load_json(PRODUCER_GRAPH_PATH, {}) if incremental else {}
    prev_seeds = prev.get("seeds", {}) if prev.get("edges") is not None else {}

    # --- liste des graines : (seed_key canonique, nom, rid ou None) ---
    seeds, seen = [], set()
    if mode == "global":
        for nk, e in ares.items():
            rid = e.get("discogs_id")
            if rid and e.get("status") in ("exact", "approx", "confirmed"):
                sk = f"id:{rid}"
                if sk in seen or (incremental and sk in prev_seeds):
                    continue
                seen.add(sk)
                seeds.append((sk, e.get("discogs_name") or e.get("original") or nk, rid))
    elif mode == "taste":
        cats = cfg.get("artist_categories", {})
        names = [n for cid in ("1", "2") for n in cats.get(cid, [])]
        seen_names = {normalize_label(n) for n in names}
        for r in load_json(CORPUS_PATH, []):
            a = (r.get("artist") or "").strip()
            k = normalize_label(a)
            if a and k and k not in seen_names and k not in ARTIST_STOPWORDS:
                seen_names.add(k)
                names.append(a)
        for nm in names:
            sk = _canon_key(nm, ares)
            if sk in seen or (incremental and sk in prev_seeds):
                continue
            seen.add(sk)
            e = ares.get(normalize_label(nm)) or {}
            seeds.append((sk, e.get("discogs_name") or nm, e.get("discogs_id") or e.get("id")))
    else:
        scored, _tier, _disp = _seed_artists(cfg)
        explicit = params.get("seed_names")
        names = ([n for n in explicit if n and n.strip()] if explicit
                 else [d for _k, d, _s in scored[:int(params.get("seeds", 40))]])
        for nm in names:
            sk = _canon_key(nm, ares)
            if sk in seen:
                continue
            seen.add(sk)
            e = ares.get(normalize_label(nm)) or {}
            seeds.append((sk, e.get("discogs_name") or nm, e.get("discogs_id") or e.get("id")))

    if incremental and prev_seeds and not seeds:
        return job.finish("Graphe déjà à jour — aucun nouvel artiste résolu depuis le dernier build.")
    if len(seeds) < 2 and not (incremental and prev_seeds):
        # pas une erreur : un compte sans goût renseigné n'a simplement rien à
        # cartographier, et une erreur le faisait réenfiler toutes les heures.
        return job.finish(f"Rien à construire : {len(seeds)} graine(s), il en faut au moins 2.")

    # en incrémental : on repart des arêtes existantes et on ajoute celles des nouvelles graines
    art_edges = prev.get("edges", {}) if incremental else {}
    lab_edges = prev.get("label_edges", {}) if incremental else {}
    seed_names = dict(prev_seeds) if incremental else {}
    seed_names.update({sk: nm for sk, nm, _ in seeds})
    seed_set = set(seed_names)
    job.st["total"] = len(seeds)
    job.msg(f"{len(seeds)} graine(s) à traiter"
            + (" (mise à jour)" if incremental and prev_seeds else " — graphe global"))
    n_resolved = 0

    faits = dict(prev_seeds) if incremental else {}   # graines réellement traitées

    def _sauver(partiel, graines):
        """Écrit le graphe. `partiel` : point de reprise (toutes les 25 graines et à
        l'arrêt) — `seeds` ne liste alors que les graines faites, pour que la reprise
        traite exactement les autres. Les graines ne figurent jamais comme candidats."""
        save_json(PRODUCER_GRAPH_PATH, {
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "n_resolved_seeds": len(graines),
            "seeds": dict(graines),
            "edges": {k: v for k, v in art_edges.items() if k not in graines},
            "label_edges": lab_edges,
            "partial": partiel,
        })

    # Budget de temps : le job rend la main (et libère le worker pour un clic
    # utilisateur) au lieu de tenir des heures ; la reprise se fait au passage suivant.
    budget = float(params.get("budget_s") or os.environ.get("RADAR_GRAPH_BUDGET_S", 2700))
    t_boucle = time.monotonic()
    interrompu = False

    con = dd.connect_readonly()
    try:
        for i, (sk, name, rid) in enumerate(seeds):
            if job.stopped() or time.monotonic() - t_boucle > budget:
                interrompu = True
                break
            if not rid:
                dn, did, _status, _cands = dd.resolve_name(name, "artist", con) if con else (None, None, None, [])
                if did:
                    rid = did
                    ares[normalize_label(name)] = {"original": name, "discogs_name": dn,
                                                   "discogs_id": rid, "status": "exact", "candidates": []}
                    save_json(ARTISTS_RESOLVED_PATH, ares)
                else:
                    r = discogs_search(token, type="artist", q=name, per_page=3).get("results", [])
                    if r:
                        rid = r[0].get("id")
                        ares[normalize_label(name)] = {"original": name, "discogs_name": r[0].get("title"),
                                                       "discogs_id": rid, "status": "approx",
                                                       "candidates": []}
                        save_json(ARTISTS_RESOLVED_PATH, ares)
                    time.sleep(1.1)
            if not rid:
                job.tick(f"{name} — introuvable")
                faits[sk] = name
                continue
            n_resolved += 1
            if con:
                n_rels = _graph_edges_from_sql(con, rid, sk, seed_set, max_credits, rw_by_role,
                                                art_edges, lab_edges, max_levels, level_decay, node_cap)
            else:
                n_rels = _graph_edges_from_api(token, rid, sk, seed_set, ares, pages, max_credits,
                                                rw_by_role, art_edges, lab_edges)
                time.sleep(1.1)
            job.tick(f"{name} — {n_rels} sortie(s)")
            faits[sk] = name
            if len(faits) % 25 == 0:
                _sauver(True, faits)
    finally:
        if con:
            con.close()

    if interrompu:
        _sauver(True, faits)
        job.st["partial"] = True
        return job.finish(f"partiel : {len(faits)}/{len(seed_names)} graines, "
                          "reprise au prochain passage")
    job.st["partial"] = False

    if not art_edges or (n_resolved == 0 and not (incremental and prev_seeds)):
        return job.finish(error=f"Graphe vide : {n_resolved}/{len(seeds)} graines résolues.")
    # un artiste devenu graine ne doit plus figurer comme candidat
    for sk in list(art_edges):
        if sk in seed_set:
            del art_edges[sk]
    total_seeds = len(seed_names)
    _sauver(False, seed_names)
    tag ="mise à jour" if incremental and prev_seeds else mode
    job.finish(f"{tag} · +{n_resolved} graine(s) traitée(s), {total_seeds} au total → "
               f"{len(art_edges)} artistes liés, {len(lab_edges)} labels.")


def job_resolve_artists(job, params):
    """Associe chaque artiste (liste manuelle par défaut) à son id Discogs.
    Écrit artists_resolved.json : {clé: {original, discogs_name, discogs_id, status,
    candidates}} avec status exact / approx / not_found."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    names = params.get("names")
    if not names:
        cats = cfg.get("artist_categories", {})
        names = [n for cid in ("1", "2", "3") for n in cats.get(cid, [])]
        if params.get("scope") == "all":
            for r in load_json(CORPUS_PATH, []):
                if r.get("artist"):
                    names.append(r["artist"])
            for a in load_json(COLLECTION_CACHE_PATH, {}).get("artist_counts", {}):
                names.append(a)
    names = [n for n in names if normalize_label(n) not in ARTIST_STOPWORDS]
    force = params.get("force", False)
    seen, todo = set(), []
    for n in names:
        k = normalize_label(n)
        if not k or k in seen:
            continue
        seen.add(k)
        cur = ares.get(k)
        if force or not cur or cur.get("status") not in ("exact", "confirmed"):
            todo.append(n)
    job.st["total"] = len(todo)
    for i, name in enumerate(todo):
        if job.stopped():
            break
        d = discogs_search(token, type="artist", q=name, per_page=8)
        cands = [{"name": c.get("title"), "id": c.get("id")}
                 for c in d.get("results", []) if c.get("title")]
        k = normalize_label(name)
        if not cands:
            ares[k] = {"original": name, "discogs_name": None, "discogs_id": None,
                       "status": "not_found", "candidates": []}
        else:
            tgt = normalize_label(name)
            exact = next((c for c in cands if normalize_label(c["name"]) == tgt), None)
            top = exact or cands[0]
            ares[k] = {"original": name, "discogs_name": top["name"], "discogs_id": top["id"],
                       "status": "exact" if exact else "approx", "candidates": cands}
        job.tick(f"{name} → {ares[k]['discogs_name'] or '?'} ({ares[k]['status']})")
        if (i + 1) % 10 == 0:
            save_json(ARTISTS_RESOLVED_PATH, ares)
        time.sleep(1.1)
    save_json(ARTISTS_RESOLVED_PATH, ares)
    ex = sum(1 for v in ares.values() if v.get("status") == "exact")
    ap = sum(1 for v in ares.values() if v.get("status") == "approx")
    nf = sum(1 for v in ares.values() if v.get("status") == "not_found")
    job.finish(f"{len(ares)} artistes ({ex} exacts, {ap} approx à vérifier, {nf} introuvables).")
