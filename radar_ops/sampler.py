"""Débit et temps restant des jobs, dérivés d'instantanés successifs.

Un fichier de statut ne contient qu'un compteur (`done`, `total`) à l'instant
T : aucun historique, donc aucun débit lisible directement. Ce module garde en
mémoire une courte fenêtre de points par job et en déduit la cadence.

Tout est en mémoire, volontairement : perdre l'historique au redémarrage du
conteneur est sans conséquence (il se reconstitue en quelques secondes), alors
qu'écrire un fichier de plus sur `/data` ajouterait un risque de saturation
pour une donnée jetable.
"""
import threading
import time
from collections import deque


class Sampler:
    def __init__(self, window_s=600.0, max_points=240):
        self.window_s = window_s
        self.max_points = max_points
        self._points = {}                 # (uid, name) -> deque[(ts, done, total)]
        self._lock = threading.Lock()

    def observe(self, statuses, now=None):
        # `now or time.time()` écraserait un instant 0.0 (zéro est faux en
        # Python) : les tests passent un temps explicite pour piloter la fenêtre.
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        with self._lock:
            for s in statuses:
                uid, name = s.get("uid"), s.get("name")
                if not uid or not name:
                    continue
                done = s.get("done")
                if not isinstance(done, (int, float)) or isinstance(done, bool):
                    continue              # statut sans compteur : rien à mesurer
                total = s.get("total") or 0
                key = (uid, name)
                pts = self._points.setdefault(key, deque(maxlen=self.max_points))
                # Un compteur qui RECULE signale un nouveau run du même job.
                # Sans cette purge, l'écart négatif produirait un débit négatif
                # puis un temps restant absurde.
                if pts and done < pts[-1][1]:
                    pts.clear()
                pts.append((now, done, total))
                while pts and pts[0][0] < cutoff:
                    pts.popleft()

    def rate(self, uid, name):
        """Éléments par seconde sur la fenêtre, ou None si indéterminable."""
        with self._lock:
            pts = self._points.get((uid, name))
            if not pts or len(pts) < 2:
                return None
            t0, d0, _ = pts[0]
            t1, d1, _ = pts[-1]
            dt = t1 - t0
            if dt <= 0:
                return None
            return (d1 - d0) / dt

    def eta_s(self, uid, name, done, total):
        if not total or total <= 0:
            return None
        r = self.rate(uid, name)
        if r is None or r <= 0:
            return None                   # à l'arrêt : annoncer une fin serait mentir
        remaining = total - done
        return 0.0 if remaining <= 0 else remaining / r

    def series(self, uid, name):
        with self._lock:
            pts = self._points.get((uid, name))
            return [(ts, done) for ts, done, _ in pts] if pts else []

    def forget_idle(self, active_keys):
        """Borne la mémoire : un job disparu de `/data/jobs` ne reviendra pas."""
        active = set(active_keys)
        with self._lock:
            for key in [k for k in self._points if k not in active]:
                self._points.pop(key, None)
