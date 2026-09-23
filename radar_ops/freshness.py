"""Les scores précalculés tiennent-ils compte de la méthode de scoring COURANTE ?

Le précalcul (`crate_jobs.job_scorestore_releases`) estampille la base de
l'utilisateur avec l'empreinte de ses entrées — la configuration de scoring et
l'état des fichiers dont `Ctx` dépend, telle que la renvoie
`scoring.derived_key()`. Comparer cette estampille à l'empreinte du moment
répond exactement à la question posée : les scores affichés sortent-ils de la
dernière méthode demandée, ou de la précédente ?

Ce module n'importe PAS `scoring` : l'empreinte courante lui est passée en
paramètre (`scoring.derived_fingerprint`). C'est ce qui lui permet d'être testé
hors ligne, sans données ni volume monté.
"""
import json
import os
import sqlite3

STATE_ABSENT = "absent"
STATE_OK = "a_jour"
STATE_STALE = "perime"


def read_meta(db_path):
    """Estampille enregistrée, ou {} si la base ou la table est absente.

    Ouverture par URI en `mode=ro` : un `sqlite3.connect(chemin)` ordinaire
    CRÉERAIT une base vide si le fichier n'existe pas encore."""
    if not os.path.isfile(db_path):
        return {}
    con = None
    try:
        uri = "file:" + db_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        return {k: v for k, v in con.execute("SELECT key, value FROM scorestore_meta")}
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()


def explain_diff(stored_detail, current_detail):
    """Ce qui a changé, en clair. Sans cela, une page de diagnostic ne dirait
    que « l'empreinte diffère », ce qui n'aide personne à décider s'il faut
    relancer le précalcul."""
    out = []
    if stored_detail.get("config") != current_detail.get("config"):
        out.append("la configuration de scoring a changé")
    names = sorted((set(stored_detail) | set(current_detail)) - {"config"})
    for name in names:
        before = stored_detail.get(name)
        after = current_detail.get(name)
        if before == after:
            continue
        if before is None:
            out.append(f"{name} est apparu")
        elif after is None:
            out.append(f"{name} a disparu")
        else:
            out.append(f"{name} a été réécrit")
    return out


def compare(db_path, current_fp, current_detail=None):
    """État de fraîcheur d'une base de scores précalculés."""
    meta = read_meta(db_path)
    stored_fp = meta.get("fingerprint")
    rows = meta.get("rows")
    try:
        rows = int(rows) if rows is not None else None
    except (TypeError, ValueError):
        rows = None

    result = {"stored_fp": stored_fp, "current_fp": current_fp,
              "computed_at": meta.get("computed_at"), "code_sha": meta.get("code_sha"),
              "rows": rows, "changes": []}

    if not stored_fp:
        result.update(state=STATE_ABSENT,
                      reason="aucun précalcul estampillé : les scores datent d'avant "
                             "la mise en place du suivi, ou n'ont jamais été calculés")
        return result

    if stored_fp == current_fp:
        result.update(state=STATE_OK, reason=None)
        return result

    changes = []
    stored_detail = meta.get("detail")
    if stored_detail and current_detail:
        try:
            changes = explain_diff(json.loads(stored_detail), current_detail)
        except ValueError:
            changes = []
    result.update(state=STATE_STALE, changes=changes,
                  reason="; ".join(changes) if changes
                         else "les entrées du scoring ont changé depuis le dernier précalcul")
    return result
