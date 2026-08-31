"""Boucle de feedback 👍/👎 + ajustement des poids (régression logistique maison).
Écrit dans reco_feedback.json — même format que l'appli Streamlit."""
import hashlib
import math
from datetime import datetime

from . import paths, store

FEAT = {"label": ("collection", "corpus", "artist", "affinity"),
        "album": ("label", "artist", "style")}
SUBKEY = {"label": "reco", "album": "album"}


def log(kind, key, name, verdict, score, feat):
    fb = store.load(paths.FEEDBACK, [])
    for e in reversed(fb):
        if e.get("kind") == kind and e.get("key") == key:
            if e.get("verdict") == verdict:
                return
            break
    keys = FEAT.get(kind, tuple(feat or {}))
    fb.append({"ts": datetime.now().isoformat(timespec="seconds"),
               "kind": kind, "key": key, "name": name, "verdict": verdict,
               "score_shown": score,
               "feat": {k: round(float((feat or {}).get(k) or 0.0), 4) for k in keys}})
    store.save(paths.FEEDBACK, fb)


def _sig(z):
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def fit(X, y, prior, l2=2.0, iters=600, lr=0.4):
    keys = list(prior.keys())
    w = [float(prior[k]) for k in keys]
    b = 0.0
    n = max(len(y), 1)
    for _ in range(iters):
        gw = [0.0] * len(keys)
        gb = 0.0
        for xi, yi in zip(X, y):
            p = _sig(b + sum(w[j] * xi.get(keys[j], 0.0) for j in range(len(keys))))
            err = p - yi
            for j in range(len(keys)):
                gw[j] += err * xi.get(keys[j], 0.0)
            gb += err
        for j in range(len(keys)):
            w[j] -= lr * (gw[j] / n + l2 * (w[j] - prior[keys[j]]) / n)
        b -= lr * (gb / n)
    w = [max(0.0, v) for v in w]
    sp, sn = sum(prior.values()), (sum(w) or 1.0)
    return {keys[j]: round(w[j] * sp / sn, 3) for j in range(len(keys))}


def summary(scoring, min_fb=12, min_cls=3):
    fb = store.load(paths.FEEDBACK, [])
    out = {}
    for kind, keys in FEAT.items():
        latest = {}
        for e in fb:
            if e.get("kind") == kind and e.get("verdict") in ("up", "down"):
                latest[e["key"]] = e
        rows = list(latest.values())
        ups = [e for e in rows if e["verdict"] == "up"]
        downs = [e for e in rows if e["verdict"] == "down"]
        prior = {k: float(scoring[SUBKEY[kind]].get(k, 0.3)) for k in keys}
        feat_tbl = []
        for k in keys:
            mu = sum(e["feat"].get(k, 0) for e in ups) / len(ups) if ups else 0
            md = sum(e["feat"].get(k, 0) for e in downs) / len(downs) if downs else 0
            feat_tbl.append({"k": k, "up": round(mu, 3), "down": round(md, 3),
                             "gap": round(mu - md, 3)})
        proposal = None
        if len(rows) >= min_fb and len(ups) >= min_cls and len(downs) >= min_cls:
            X = [e["feat"] for e in rows]
            yv = [1 if e["verdict"] == "up" else 0 for e in rows]
            prop = fit(X, yv, prior)
            proposal = [{"k": k, "cur": round(prior[k], 3), "new": prop[k],
                         "d": round(prop[k] - prior[k], 3)} for k in keys]
        out[kind] = {"subkey": SUBKEY[kind], "n": len(rows), "up": len(ups), "down": len(downs),
                     "feat": feat_tbl, "proposal": proposal,
                     "need": max(min_fb - len(rows), 0)}
    return out
