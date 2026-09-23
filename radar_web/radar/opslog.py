"""Journal append-only des actions, écrit par l'appli web et relu par `radar_ops`.

Un seul module pour l'écriture ET la lecture : le format d'une ligne est un
contrat entre deux processus, et deux copies du parseur auraient divergé à la
première évolution du format.

Une ligne = un objet JSON. L'écriture se fait en UN seul `write()` sur un
descripteur ouvert en `a` : sous POSIX, une écriture de moins de PIPE_BUF sur
un fichier en mode append ne s'entrelace pas avec celle d'un autre processus,
ce qui permet au web et au worker d'écrire dans le même fichier sans verrou.
"""
import json
import os
import time

from . import paths

LOG_PATH = os.path.join(paths.DATA, "ops", "actions.jsonl")

# Le VPS a déjà saturé deux fois à cause de fichiers de trace non bornés
# (cf. CLAUDE.md pt 12) : la rotation n'est pas un raffinement, c'est la
# condition pour que ce journal ait le droit d'exister.
MAX_BYTES = 5 * 1024 * 1024
_ROTATE_CHECK_EVERY = 200
_writes_since_check = 0

# Requêtes de fond qui noieraient le journal sans rien apprendre : sondage de
# statut des jobs, autocomplétion, fichiers statiques, flux temps réel.
_NOISY_PREFIXES = ("/static", "/suggest/", "/ops/stream")
_NOISY_SUFFIXES = ("/status",)
_NOISY_EXACT = frozenset({"/health", "/favicon.ico"})


def is_noisy(path):
    return (path in _NOISY_EXACT
            or path.startswith(_NOISY_PREFIXES)
            or path.endswith(_NOISY_SUFFIXES))


def append(path, event):
    """Ajoute un événement. N'échoue JAMAIS : journaliser est un effet de bord,
    jamais une raison de faire échouer la requête de l'utilisateur."""
    global _writes_since_check
    p = path
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        e = dict(event)                      # jamais muter le dict de l'appelant
        e.setdefault("ts", time.time())
        line = json.dumps(e, ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
        _writes_since_check += 1
        # `os.path.getsize` à chaque ligne coûterait un appel système par requête
        # HTTP servie : on ne vérifie que périodiquement.
        if _writes_since_check >= _ROTATE_CHECK_EVERY:
            _writes_since_check = 0
            rotate_if_needed(p)
    except Exception:
        pass


def record(method, path, status, uid, duration_s):
    """Trace une requête servie, sauf si elle relève du bruit de fond.

    Seuls la méthode, le chemin et le code de retour sont retenus : jamais la
    chaîne de requête ni le corps, qui peuvent porter un mot de passe
    (`/login`) ou un jeton Discogs (`/settings`)."""
    if is_noisy(path):
        return
    append(LOG_PATH, {"kind": "http", "uid": uid, "method": method, "path": path,
                      "status": int(status), "ms": round(duration_s * 1000)})


def rotate_if_needed(path, max_b=MAX_BYTES):
    """Bascule le journal en `<path>.1` au-delà de `max_b`. Un seul fichier de
    secours conservé : ce journal sert au diagnostic du moment, pas à
    l'archivage."""
    p = path
    try:
        if os.path.getsize(p) <= max_b:
            return False
        os.replace(p, p + ".1")              # atomique, écrase le précédent
        return True
    except OSError:
        return False


def _decode(chunk):
    out = []
    for raw in chunk.split(b"\n"):
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue                          # ligne tronquée par une rotation
        if isinstance(ev, dict):
            out.append(ev)
    return out


def tail(path, limit=200, since_ts=None):
    """Derniers événements, du plus récent au plus ancien.

    Remonte le fichier par blocs depuis la fin : le journal peut peser
    plusieurs mégaoctets et la page de diagnostic n'en affiche qu'une poignée
    de lignes."""
    p = path
    events = []
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            offset = f.tell()
            tail_bytes = b""
            while offset > 0 and len(events) < limit:
                step = min(65536, offset)
                offset -= step
                f.seek(offset)
                chunk = f.read(step) + tail_bytes
                lines = chunk.split(b"\n")
                # Tant qu'on n'a pas atteint le début du fichier, la première
                # ligne du bloc est coupée en deux : on la garde pour le tour
                # suivant plutôt que de la jeter.
                tail_bytes = lines[0] if offset > 0 else b""
                for raw in reversed(lines[1:] if offset > 0 else lines):
                    for ev in _decode(raw):
                        if since_ts is not None and ev.get("ts", 0) <= since_ts:
                            return events
                        events.append(ev)
                        if len(events) >= limit:
                            return events
    except OSError:
        pass
    return events


def stream_new(path, from_offset):
    """Événements ajoutés depuis `from_offset`, et le nouvel offset.

    Alimente le flux temps réel sans relire tout le fichier à chaque tick. Un
    fichier plus court qu'attendu signale une rotation : on repart de zéro
    plutôt que de lire au milieu d'une ligne."""
    p = path
    try:
        size = os.path.getsize(p)
    except OSError:
        return [], 0
    if from_offset > size:
        from_offset = 0
    try:
        with open(p, "rb") as f:
            f.seek(from_offset)
            data = f.read()
    except OSError:
        return [], from_offset
    if not data:
        return [], from_offset
    # Une ligne sans retour final est en cours d'écriture : on laisse l'offset
    # avant elle pour la relire entière au prochain tick.
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], from_offset
    return _decode(data[:cut]), from_offset + cut + 1
