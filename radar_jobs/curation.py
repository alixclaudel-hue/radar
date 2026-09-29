"""Curation des labels/artistes suivis : `enrich`, `canonicalize`, `prune_labels`.
"""

import os
import time
from collections import Counter
from datetime import datetime

from radar_web.radar import store
from radar_jobs.common import (
    ARTISTS_RESOLVED_PATH, cfg_load, CONFIG_PATH, CORPUS_PATH, load_json, normalize_label,
    PENDING_ENRICH_PATH, PROFILE_PATH, RADAR_UID, RESOLVED_PATH, save_json, USER_DIR,
)
from radar_jobs.sources import discogs_get, discogs_search


def _resolve_entity(token, name, kind):
    """kind='label'|'artist' -> (discogs_name, id, status, candidates).

    Essaie d'abord le référentiel local (radar/discogs_dump.py, importé du
    dump mensuel) — sans appel API ni pause. Ne retombe sur la recherche API
    (avec la pause habituelle) que si le nom est absent du dump : pas encore
    importé, ou vraiment introuvable dans le catalogue Discogs. Un nom
    retrouvé localement (correspondance directe ou via une variante de
    graphie du dump artistes) est marqué 'exact' : ni l'un ni l'autre ne
    devine, contrairement à la recherche API floue."""
    from radar_web.radar import discogs_dump as dd
    dn, did, status, cands = dd.resolve_name(name, kind)
    if did:
        return dn, did, "exact", []
    if status == "ambiguous" and cands:
        # plusieurs entités Discogs partagent cette name_key (suffixes de désambiguïsation
        # "(2)"/"(3)" retirés par normalize_label) -> jamais un id choisi au hasard comme
        # 'exact', remonté comme 'approx' avec les candidats pour la revue C3 (diagnostic R3).
        return None, None, "approx", cands
    d = discogs_search(token, type=kind, q=name, per_page=8)
    time.sleep(1.1)
    cands = [{"name": c.get("title"), "id": c.get("id")}
             for c in d.get("results", []) if c.get("title")]
    if not cands:
        return None, None, "not_found", []
    tgt = normalize_label(name)
    exact = next((c for c in cands if normalize_label(c["name"]) == tgt), None)
    top = exact or cands[0]
    return top["name"], top["id"], ("exact" if exact else "approx"), cands


def _profile_label(token, name):
    d = discogs_search(token, label=name, per_page=100, page=1, sort="want", sort_order="desc")
    res = d.get("results", [])
    sc, gc = Counter(), Counter()
    for x in res:
        sc.update(x.get("style") or [])
        gc.update(x.get("genre") or [])
    return {"original": name, "sampled": len(res), "style_counts": dict(sc),
            "genre_counts": dict(gc),
            "total_items": d.get("pagination", {}).get("items", len(res)),
            "profiled_at": datetime.now().isoformat(timespec="seconds")}


def _rename_in_list(lst, old, new):
    """Remplace old -> new dans lst (comparaison normalisée), dédup. True si changé."""
    if not new or normalize_label(old) == normalize_label(new):
        return False
    out, changed, seen = [], False, set()
    for x in lst:
        y = new if normalize_label(x) == normalize_label(old) else x
        changed = changed or (y != x)
        if normalize_label(y) not in seen:
            seen.add(normalize_label(y))
            out.append(y)
    lst[:] = out
    return changed


def job_enrich(job, params):
    """Draine pending_enrich.json : pour chaque label/artiste fraîchement ajouté,
    résolution vers le nom Discogs canonique (+ id) puis profilage (labels).
    Renomme au passage la base / la veille / les catégories."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    pend = load_json(PENDING_ENRICH_PATH, {})
    labels = list(dict.fromkeys(pend.get("labels", [])))
    artists = list(dict.fromkeys(pend.get("artists", [])))
    if not token:
        return job.finish(error="Pas de token Discogs.")
    job.st["total"] = len(labels) + len(artists)
    if not job.st["total"]:
        return job.finish("Rien à enrichir.")

    res = load_json(RESOLVED_PATH, {})
    prof = load_json(PROFILE_PATH, {})
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    cfg_changed = False

    for name in labels:
        if job.stopped():
            break
        dn, did, status, cands = _resolve_entity(token, name, "label")
        canon = dn if (dn and status == "exact") else name
        res[normalize_label(name)] = {"original": name, "discogs_name": dn or name,
                                      "discogs_id": did, "status": status,
                                      "candidates": cands if status in ("approx", "not_found") else [],
                                      "reviewed_by": "auto"}
        if canon != name:
            lcats = cfg.setdefault("label_categories", {"1": [], "2": []})
            for cid in ("1", "2"):
                cfg_changed |= _rename_in_list(lcats.setdefault(cid, []), name, canon)
            res[normalize_label(canon)] = res[normalize_label(name)]
        pk = normalize_label(canon)
        if pk not in prof:
            prof[pk] = _profile_label(token, canon)
            time.sleep(1.1)
        save_json(RESOLVED_PATH, res)
        save_json(PROFILE_PATH, prof)
        job.tick(f"label {name} → {dn or '?'} ({status})")

    for name in artists:
        if job.stopped():
            break
        dn, did, status, cands = _resolve_entity(token, name, "artist")
        ares[normalize_label(name)] = {"original": name, "discogs_name": dn or name,
                                       "discogs_id": did, "status": status,
                                       "candidates": cands if status in ("approx", "not_found") else []}
        if dn and status == "exact" and normalize_label(dn) != normalize_label(name):
            for cid in ("1", "2", "3"):
                cfg_changed |= _rename_in_list(
                    cfg.setdefault("artist_categories", {}).setdefault(cid, []), name, dn)
        save_json(ARTISTS_RESOLVED_PATH, ares)
        job.tick(f"artiste {name} → {dn or '?'} ({status})")

    if cfg_changed:
        save_json(CONFIG_PATH, cfg)
    save_json(PENDING_ENRICH_PATH, {"labels": [], "artists": []})
    job.finish(f"+{len(labels)} label(s), +{len(artists)} artiste(s) enrichis.")


def job_canonicalize(job, params):
    """Passe unique : réécrit toute la base (labels + catégories d'artistes) avec les
    noms Discogs canoniques + ids, puis les champs artiste/label du corpus.

    Résolution locale d'abord (radar/discogs_dump.py resolve_name/lookup_release) :
    plus de token nécessaire pour l'essentiel, plus de pause. Un passage complet
    prend maintenant quelques secondes au lieu de plusieurs heures dès que le
    référentiel local couvre la base — canonicalize.state.json (dont la seule
    raison d'être était de ne pas repayer des appels API coûteux en cas
    d'interruption) a donc disparu : on repart de zéro à chaque lancement, un
    nom déjà résolu 'exact'/'confirmed' étant de toute façon sauté (cf. le test
    juste en dessous). Un token reste utile en repli pour les noms absents du
    référentiel local (pas encore importés, ou introuvables sur Discogs).
    `params['scope']` = 'names' | 'corpus' (défaut 'corpus')."""
    from radar_web.radar import discogs_dump as dd

    try:                     # reliquat de l'ancien mécanisme (cf. docstring) : plus lu
        os.remove(os.path.join(USER_DIR, "canonicalize.state.json"))  # nulle part, best-effort
    except OSError:
        pass

    cfg = cfg_load()
    token = cfg.get("token", "")
    scope = params.get("scope", "corpus")

    res = load_json(RESOLVED_PATH, {})
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    lcats = cfg.setdefault("label_categories", {"1": [], "2": []})
    label_names = [n for cid in ("1", "2") for n in lcats.get(cid, [])]
    cats = cfg.setdefault("artist_categories", {})
    art_names = [n for cid in ("1", "2", "3") for n in cats.get(cid, [])]
    corpus = load_json(CORPUS_PATH, []) if scope == "corpus" else []
    corpus_todo = [r for r in corpus if r.get("release_id")]
    job.st["total"] = len(label_names) + len(art_names) + len(corpus_todo)

    changed, n_since_save = 0, 0
    for name in label_names:
        if job.stopped():
            break
        k = normalize_label(name)
        cur = res.get(k)
        if cur and cur.get("discogs_id") and cur.get("status") in ("exact", "confirmed"):
            canon = cur.get("discogs_name") or name
            if cur.get("candidates"):            # migration C2 : plus persisté pour exact/confirmed
                cur["candidates"] = []
        else:
            dn, did, status, cands = _resolve_entity(token, name, "label")
            res[k] = {"original": name, "discogs_name": dn or name, "discogs_id": did,
                      "status": status, "candidates": cands if status in ("approx", "not_found") else [],
                      "reviewed_by": "auto"}
            canon = dn if (dn and status == "exact") else name
        renamed = False
        for cid in ("1", "2"):
            renamed = _rename_in_list(lcats.setdefault(cid, []), name, canon) or renamed
        if renamed:
            changed += 1
            res[normalize_label(canon)] = res.get(k, {})
        n_since_save += 1
        if n_since_save >= 200:                   # sauvegarde périodique : survit à une interruption
            save_json(CONFIG_PATH, cfg)             # (pas d'appel API pour un nom déjà en 'exact'/'confirmed')
            save_json(RESOLVED_PATH, res)
            n_since_save = 0
        job.tick(f"label {name} → {canon}")
    save_json(CONFIG_PATH, cfg)
    save_json(RESOLVED_PATH, res)

    n_since_save = 0
    for name in list(art_names):
        if job.stopped():
            break
        k = normalize_label(name)
        cur = ares.get(k)
        if cur and cur.get("discogs_id") and cur.get("status") in ("exact", "confirmed"):
            canon = cur.get("discogs_name") or name
            if cur.get("candidates"):            # migration C2 : plus persisté pour exact/confirmed
                cur["candidates"] = []
        else:
            dn, did, status, cands = _resolve_entity(token, name, "artist")
            ares[k] = {"original": name, "discogs_name": dn or name, "discogs_id": did,
                       "status": status, "candidates": cands if status in ("approx", "not_found") else []}
            canon = dn if (dn and status == "exact") else name
        for cid in ("1", "2", "3"):
            _rename_in_list(cats.setdefault(cid, []), name, canon)
        n_since_save += 1
        if n_since_save >= 200:
            save_json(CONFIG_PATH, cfg)
            save_json(ARTISTS_RESOLVED_PATH, ares)
            n_since_save = 0
        job.tick(f"artiste {name} → {canon}")
    save_json(CONFIG_PATH, cfg)
    save_json(ARTISTS_RESOLVED_PATH, ares)

    n_corpus = 0
    if corpus_todo:
        con = dd.connect_readonly()
        try:
            for r in corpus_todo:
                if job.stopped():
                    break
                ref = dd.lookup_release(r["release_id"], con) if con else None
                if ref:
                    if ref.get("artist"):
                        r["artist"] = ref["artist"]
                    if ref.get("label"):
                        r["label"] = ref["label"]
                elif token and not con:
                    # repli API seulement si le référentiel local n'est pas encore importé.
                    # Si `con` existe mais que le lookup échoue, c'est presque toujours une
                    # sortie non-vinyle (filtrée hors du dump par _is_vinyl) : sans ce garde-fou,
                    # cette ligne repayait un appel API + sleep(1.1) À CHAQUE passage, pour
                    # toujours — canonicalize tournant désormais en auto hebdo (diagnostic R1).
                    d = discogs_get(token, f"/releases/{r['release_id']}")
                    if d:
                        arts = d.get("artists") or []
                        if arts:
                            r["artist"] = ", ".join(a.get("name", "").strip() for a in arts if a.get("name"))
                        labs = d.get("labels") or []
                        if labs:
                            r["label"] = labs[0].get("name") or r.get("label")
                    time.sleep(1.1)
                n_corpus += 1
                job.tick(f"corpus {r.get('artist', '')[:30]}")
        finally:
            if con:
                con.close()
        save_json(CORPUS_PATH, corpus)

    job.finish(f"Nettoyage : {changed} label(s) renommé(s), {len(art_names)} artiste(s), "
               f"{n_corpus} ligne(s) de corpus canonisées.")


def job_prune_labels(job, params):
    """Élague `label_categories` (Cœur+Aimé) des labels hors du goût courant
    (`taste_categories`) — chantier C du pt 57 (CLAUDE.md), brief révisé par la
    session VPS le 17/09. But : PERTINENCE du signal `artist_label_signal`/
    `label_db_signal`, pas performance (l'élagage seul ne gagne que ~4% sur
    `artist_ids_for_labels` — le vrai coût vient des catalogues géants, sujet
    séparé, cf. CLAUDE.md pt 57).

    Pour chaque label de label_categories (1 ou 2) :
      st    = discogs_dump.label_style_counts([key]), repli Ctx.profile[key]
      tot   = somme des valeurs de st (majorant : une sortie peut porter
              plusieurs styles, cf. note ci-dessous)
      known = somme des valeurs de st dont style_key(style) est dans Ctx.wmap
      pct   = 100 * known / tot (0 si tot == 0)
    Retiré si (known < max_known) OU (pct < min_pct), SAUF label possédé
    (collection.label_counts > 0) ou écouté (corpus_label_scores > 0) —
    ces deux garde-fous ne sont PAS désactivables par paramètre.

    `known`/`tot` comptent des sorties par STYLE, une même sortie multi-style
    peut donc être comptée plusieurs fois : majorant, pas un nombre exact de
    disques — ne jamais l'afficher comme tel.

    SIMULATION PAR DÉFAUT (apply=False) : calcule et journalise sans écrire.
    `apply=True` retire les entrées de la config (suppression sèche, décision
    utilisateur 17/09 — pas de catégorie "écarté" réversible) après avoir
    copié crate_radar_config.json vers un .bak-<horodatage> et écrit un
    rapport JSON listant les labels retirés (nom, tier, tot, known, pct) pour
    permettre un retour en arrière manuel.

    CLI seulement (pas de bouton web, cf. CLAUDE.md pt 57 volet C) :
        python crate_jobs.py prune_labels '{"max_known": 5, "min_pct": 5}'
        python crate_jobs.py prune_labels '{"max_known": 5, "min_pct": 5, "apply": 1}'
    """
    from radar_web.radar import discogs_dump as dd
    from radar_web.radar.scoring import Ctx

    max_known = int(params.get("max_known", 5))
    min_pct = float(params.get("min_pct", 5.0))
    apply_ = bool(params.get("apply"))

    cfg = cfg_load()
    lcats = cfg.get("label_categories", {"1": [], "2": []})
    entries = [(cid, name) for cid in ("1", "2") for name in lcats.get(cid, []) if name and name.strip()]
    if not entries:
        return job.finish("Aucun label suivi (catégorie 1 ou 2) — rien à élaguer.")

    ctx = Ctx(uid=RADAR_UID)
    wmap = ctx.wmap
    owned = ctx.collection.get("label_counts", {})
    heard = ctx.corpus_label_scores()

    keys = sorted({normalize_label(n) for _, n in entries})
    style_counts = dd.label_style_counts(keys)   # découpe elle-même par lots depuis le 18/09

    job.st["total"] = len(entries)
    kept, removed = [], []
    for cid, name in entries:
        key = normalize_label(name)
        st = style_counts.get(key) or (ctx.profile.get(key) or {}).get("style_counts") or {}
        tot = sum(st.values())
        known = sum(n for s, n in st.items() if store.style_key(s) in wmap)
        pct = round(100 * known / tot, 1) if tot else 0.0
        spared = owned.get(key, 0) > 0 or heard.get(key, 0) > 0
        row = {"tier": cid, "name": name, "key": key, "tot": tot, "known": known, "pct": pct}
        drop = not spared and (known < max_known or pct < min_pct)
        (removed if drop else kept).append(row)
        job.tick(f"{name} : tot={tot} known={known} pct={pct}%" + (" — écarté" if drop else ""))

    if apply_:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak_path = f"{CONFIG_PATH}.bak-{ts}"
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
        with open(bak_path, "w", encoding="utf-8") as f:
            f.write(raw)
        removed_keys = {r["key"] for r in removed}
        for cid in ("1", "2"):
            lcats[cid] = [n for n in lcats.get(cid, []) if normalize_label(n) not in removed_keys]
        cfg["label_categories"] = lcats
        save_json(CONFIG_PATH, cfg)
        report_path = os.path.join(USER_DIR, f"prune_labels_report_{ts}.json")
        save_json(report_path, {
            "params": {"max_known": max_known, "min_pct": min_pct},
            "removed": removed, "kept_count": len(kept),
            "config_backup": bak_path})
        job.finish(f"APPLIQUÉ — {len(removed)} label(s) retiré(s), {len(kept)} gardé(s). "
                   f"Sauvegarde : {bak_path} — rapport : {report_path}")
    else:
        job.finish(f"SIMULATION — {len(removed)} label(s) à retirer, {len(kept)} gardé(s). "
                   f"Relancer avec apply=1 pour appliquer.")
