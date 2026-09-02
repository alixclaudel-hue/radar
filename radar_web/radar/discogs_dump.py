"""Référentiel local des sorties Discogs, construit à partir du dump mensuel
officiel (data.discogs.com) — catalogue/labels/artistes/genres, PAS le
marketplace (vendeurs/prix/inventaire, qui reste toujours en API live, cf.
`sellers.py`/`crate_jobs.job_scan_catalog`).

Le dump complet fait plusieurs dizaines de Go décompressés pour ~18-20M de
sorties tous formats — on ne garde que le vinyle et un sous-ensemble de
champs (id, titre, artiste, label, catno, année, pays, formats, genres,
styles, master_id) dans un fichier SQLite unique sous SHARED_DIR (pas de
serveur, un fichier comme les autres dans /data).

Rempli par le job `import_discogs_dump` (crate_jobs.py), rafraîchi par la
veille mensuelle du worker (RADAR_DISCOGS_DUMP_SYNC=1). Un dump mensuel est
un instantané complet, jamais un delta — "actualiser" retélécharge et
reconstruit l'index en entier.
"""
import gzip
import io
import json
import os
import re
import sqlite3
import time

import requests

from . import paths
from .store import normalize_label

DB_PATH = os.path.join(paths.SHARED_DIR, "discogs_dump.sqlite3")
META_PATH = os.path.join(paths.SHARED_DIR, "discogs_dump_meta.json")
RAW_DIR = os.path.join(paths.SHARED_DIR, "discogs_dump_raw")

# Le bucket S3 direct (discogs-data-dumps.s3...) renvoie 403 sur le listing
# ET sur les objets depuis fin 2025 (vérifié) — seul data.discogs.com (même
# bucket, derrière Cloudflare) fonctionne encore. Le chemin direct
# (/data/{year}/...) a lui aussi cessé de servir le fichier : le serveur y
# répond 200 + une page HTML générique au lieu du binaire (vérifié en
# conditions réelles). Le téléchargement passe désormais par un paramètre de
# requête ?download=... — c'est ce que la page de listing elle-même génère
# comme lien pour chaque fichier.
# UA de navigateur : Cloudflare peut challenger un UA "requests"/"curl" nu.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36 Radar/1.0 (+personal-use, cf. CLAUDE.md)")
DATA_HOST = "https://data.discogs.com"
DUMP_URL_TMPL = DATA_HOST + "/?download=data/{year}/discogs_{date}_releases.xml.gz"
CHECKSUM_URL_TMPL = DATA_HOST + "/?download=data/{year}/discogs_{date}_CHECKSUM.txt"

_NOT_VINYL_MARKERS = ('7"', '10"', "CD", "Cassette", "Cass", "File", "DVD", "Blu-ray", "SACD", "VHS")


def _is_vinyl(fmt_names, fmt_descriptions):
    """Restreint au 12"/LP, comme sellers._is_12in — le nom de format 'Vinyl'
    à lui seul couvre AUSSI les 7" et 10" (la taille n'est que dans les
    descriptions), donc l'exclusion doit être vérifiée avant, pas après."""
    if "Vinyl" not in fmt_names:
        return False
    text = fmt_descriptions or ""
    if any(m in text for m in _NOT_VINYL_MARKERS):
        return False
    return "LP" in text or '12"' in text


# --------------------------------------------------------------- SQLite

def _create_schema(con):
    con.execute("""
        CREATE TABLE releases (
            id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            artist_key TEXT,
            label TEXT,
            label_key TEXT,
            catno TEXT,
            year INTEGER,
            country TEXT,
            format TEXT,
            genres TEXT,
            styles TEXT,
            master_id INTEGER
        )
    """)
    # table à part (pas de LIKE '%…%' possible sur la colonne styles jointe par
    # virgule -> jamais indexable) : une ligne par (release_id, style), pour
    # pouvoir interroger un style précis par index plutôt que par SCAN complet.
    con.execute("CREATE TABLE release_styles (release_id INTEGER, style TEXT)")


def _create_indexes(con):
    """Appelé une fois la table remplie, jamais avant : indexer une table déjà
    pleine évite de mettre à jour les arbres B à chaque ligne insérée pendant
    tout l'import (facteur 2-3 sur la durée, cf. diagnostic D1)."""
    con.execute("CREATE INDEX idx_releases_label_key ON releases(label_key)")
    con.execute("CREATE INDEX idx_releases_artist_key ON releases(artist_key)")
    con.execute("CREATE INDEX idx_releases_year ON releases(year)")
    con.execute("CREATE INDEX idx_rs_style ON release_styles(style)")
    con.execute("CREATE INDEX idx_rs_release ON release_styles(release_id)")


def get_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_meta(d):
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, META_PATH)


# --------------------------------------------------------------- listing / téléchargement

def _list_via_s3_xml(year):
    """Repli 1 : tente le ListBucketResult XML standard d'un accès S3 direct.
    En pratique data.discogs.com répond désormais avec une page HTML de
    listing (cf. `_list_via_html`) — ce repli échoue donc au parsing XML et
    laisse la main au suivant, gardé au cas où la forme XML reviendrait."""
    r = requests.get(f"{DATA_HOST}/", params={"delimiter": "/", "prefix": f"data/{year}/"},
                      headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.content)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    out = []
    for contents in root.findall("s3:Contents", ns):
        key = contents.findtext("s3:Key", default="", namespaces=ns)
        m = re.search(r"discogs_(\d{8})_releases\.xml\.gz$", key)
        if m:
            out.append(m.group(1))
    return out


def _list_via_html(year):
    """Repli 2 : si la réponse n'est pas du XML S3 (page HTML derrière
    Cloudflare), on retombe sur une regex sur le corps de la page."""
    r = requests.get(f"{DATA_HOST}/", params={"prefix": f"data/{year}/"},
                      headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return list({m for m in re.findall(r"discogs_(\d{8})_releases\.xml\.gz", r.text)})


def _list_via_month_probe(months_back=6):
    """Repli 3, déterministe, sans listing : la cadence récente est le 1er du
    mois (pas garanti historiquement) — on sonde le CHECKSUM.txt de chaque
    mois récent en HEAD et on garde le plus récent qui répond avec un vrai
    fichier. Le host répond 200 + une page HTML générique pour à peu près
    n'importe quel chemin (vérifié en conditions réelles) : le status_code
    seul ne prouve plus rien, on exige en plus un en-tête `content-disposition`
    de type pièce jointe, propre aux vraies réponses de téléchargement."""
    from datetime import date
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months_back):
        date_str = f"{y}{m:02d}01"
        url = CHECKSUM_URL_TMPL.format(year=y, date=date_str)
        try:
            r = requests.head(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
            if r.status_code == 200 and "attachment" in r.headers.get("content-disposition", "").lower():
                out.append(date_str)
        except requests.RequestException:
            pass
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def find_latest_dump_date():
    """Date 'YYYYMMDD' du dernier dump 'releases' publié — chaîne de repli :
    ListBucketResult XML -> regex HTML (celui qui marche en pratique,
    vérifié en conditions réelles) -> sondage déterministe mois par mois.
    Essaie l'année courante puis la précédente (cas d'un import lancé en
    tout début d'année, avant le 1er dump de l'année)."""
    from datetime import date
    for lister in (_list_via_s3_xml, _list_via_html):
        for year in (date.today().year, date.today().year - 1):
            try:
                found = lister(year)
            except Exception:                     # noqa: BLE001 — on tente le repli suivant
                continue
            if found:
                return max(found)
    found = _list_via_month_probe()
    if found:
        return max(found)
    raise RuntimeError(
        "Impossible de déterminer le dernier dump Discogs disponible (listing et sondage "
        "ont tous les deux échoué) — vérifier que data.discogs.com est bien joignable "
        "et que son mécanisme de listing n'a pas encore changé de forme.")


def dump_url(date_str):
    return DUMP_URL_TMPL.format(year=date_str[:4], date=date_str)


def checksum_url(date_str):
    return CHECKSUM_URL_TMPL.format(year=date_str[:4], date=date_str)


def verify_checksum(date_str, file_path):
    """True/False/None (None = CHECKSUM.txt indisponible, on ne bloque pas
    l'import dessus). Format sha256sum GNU : '<hash>  <filename>' par ligne."""
    import hashlib
    try:
        r = requests.get(checksum_url(date_str), headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except requests.RequestException:
        return None
    fname = os.path.basename(file_path)
    expected = None
    for line in r.text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == fname:
            expected = parts[0].strip()
            break
    if not expected:
        return None
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def looks_like_gzip(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _diagnose_bad_download(date_str, file_path):
    """Fichier téléchargé mais pas du gzip valide (mauvaise URL, page de
    blocage Cloudflare...) : taille + aperçu du contenu + un HEAD frais sur
    l'URL attendue pour voir ce que le serveur répond maintenant."""
    try:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            head = f.read(300)
        preview = head.decode("utf-8", errors="replace").strip() or "(vide)"
    except OSError as e:
        size, preview = None, f"(illisible : {e})"
    info = f"taille={size} octet(s), aperçu={preview!r}"
    try:
        r = requests.head(dump_url(date_str), headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        info += f" — HEAD frais sur l'URL attendue : {r.status_code} ({r.headers.get('content-type', '?')})"
    except requests.RequestException as e:
        info += f" — HEAD frais échoué : {e}"
    return info


def download_dump(date_str, dest_path, progress_cb=None):
    """Téléchargement en flux (le fichier fait plusieurs Go) avec reprise
    simple : si un fichier partiel existe déjà et correspond en taille à un
    téléchargement précédent interrompu, on ne repart pas de zéro (Range).
    Un partiel qui ne commence pas par la signature gzip (page d'erreur/de
    blocage écrite là par une tentative précédente) est jeté avant de servir
    de base à une reprise, sinon on ne ferait qu'empiler du contenu valide
    après un en-tête déjà corrompu."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.exists(dest_path) and os.path.getsize(dest_path) >= 2 and not looks_like_gzip(dest_path):
        os.remove(dest_path)
    url = dump_url(date_str)
    resume_from = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
    headers = {"User-Agent": UA}
    mode = "wb"
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        mode = "ab"
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if resume_from and r.status_code == 416:          # déjà complet
            return
        if resume_from and r.status_code != 206:           # serveur ne gère pas Range -> repart de 0
            resume_from = 0
            mode = "wb"
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if resume_from == 0 and "html" in ctype:
            preview = next(r.iter_content(chunk_size=2048), b"")
            raise RuntimeError(
                f"réponse HTML au lieu du dump (content-type={ctype!r}) : {preview[:200]!r} — "
                "probablement une page de blocage/erreur, pas le fichier attendu.")
        total = resume_from + int(r.headers.get("content-length", 0))
        done = resume_from
        with open(dest_path, mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total or done)
        if total and done < total:               # connexion coupée en route (ex. reset côté serveur)
            raise RuntimeError(f"téléchargement incomplet : {done}/{total} octets reçus.")
    if resume_from == 0 and not looks_like_gzip(dest_path):
        diag = _diagnose_bad_download(date_str, dest_path)
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise RuntimeError(f"le fichier téléchargé n'est pas un gzip valide — {diag}")


# --------------------------------------------------------------- parsing

class _RootWrappedStream(io.RawIOBase):
    """Certains exports Discogs concatènent les <release>...</release> sans
    tag racine englobant (documenté par plusieurs parseurs tiers) ; d'autres
    en ont un. On enveloppe systématiquement d'un faux root pour être
    indifférent au cas rencontré — un root en trop autour d'un root déjà
    présent casserait le parsing, donc on ne wrappe QUE si le flux ne
    commence pas déjà par une balise racine plurielle après le prologue XML."""

    def __init__(self, raw, needs_wrap, skip_bytes=0):
        self._raw = raw
        self._skip = skip_bytes if needs_wrap else 0
        self._prefix = b"<discogs_dump_root>" if needs_wrap else b""
        self._suffix = b"</discogs_dump_root>" if needs_wrap else b""
        self._sent_prefix = not self._prefix
        self._sent_suffix = False
        self._eof = False

    def readable(self):
        return True

    def readinto(self, b):
        if not self._sent_prefix:
            n = min(len(b), len(self._prefix))
            b[:n] = self._prefix[:n]
            self._prefix = self._prefix[n:]
            self._sent_prefix = not self._prefix
            return n
        while self._skip > 0:                    # avale le prologue <?xml ... ?> avant le contenu
            discarded = self._raw.read(min(self._skip, 65536))
            if not discarded:
                self._skip = 0
                break
            self._skip -= len(discarded)
        if self._eof:
            if not self._sent_suffix:
                n = min(len(b), len(self._suffix))
                b[:n] = self._suffix[:n]
                self._suffix = self._suffix[n:]
                self._sent_suffix = not self._suffix
                return n
            return 0
        chunk = self._raw.read(len(b))
        if not chunk:
            self._eof = True
            return self.readinto(b)
        b[:len(chunk)] = chunk
        return len(chunk)


def _detect_needs_wrap(gz_path):
    """(needs_wrap, skip_bytes) — skip_bytes = longueur du prologue <?xml...?>
    (espaces de tête inclus) à avaler si on doit injecter notre propre root,
    pour ne pas laisser une déclaration XML au milieu du flux (invalide)."""
    with gzip.open(gz_path, "rb") as f:
        raw_head = f.read(4096)
    body = raw_head.lstrip()
    skip = len(raw_head) - len(body)
    if body.startswith(b"<?xml") and b"?>" in body:
        decl_len = body.index(b"?>") + 2
        skip += decl_len
        rest = body[decl_len:]
        skip += len(rest) - len(rest.lstrip())
        body = rest.lstrip()
    needs_wrap = not body.startswith(b"<releases")
    return needs_wrap, skip


def import_releases(gz_path, progress_cb=None, batch_size=5000):
    """Parse en flux le dump releases.xml.gz, ne garde que le vinyle, insère
    en base par lots.

    Reconstruit dans un fichier à part (`DB_PATH + ".new"`), jamais dans
    `DB_PATH` lui-même : l'appli continue de lire l'ancienne base, valide,
    jusqu'à la bascule atomique (`os.replace`) tout à la fin. Plus de fenêtre
    où `available()` mentirait (base présente mais vide/partielle pendant
    ~1h45 d'import), et un import interrompu ne détruit jamais l'existant.
    Les index sont créés APRÈS le remplissage (facteur 2-3 sur la durée d'un
    import sur une table déjà indexée). Retourne (n_total_vu, n_vinyle)."""
    import xml.etree.ElementTree as ET

    needs_wrap, skip_bytes = _detect_needs_wrap(gz_path)
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    new_path = DB_PATH + ".new"
    if os.path.exists(new_path):
        os.remove(new_path)              # reliquat d'un import précédent interrompu
    con = sqlite3.connect(new_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")     # base jetable jusqu'à la bascule : la durabilité
    con.execute("PRAGMA temp_store=MEMORY")   # de ce fichier temporaire n'a pas d'importance
    con.execute("PRAGMA cache_size=-131072")  # 128 Mo
    _create_schema(con)
    n_total, n_vinyl, batch, style_rows = 0, 0, [], []

    def flush():
        if batch:
            con.executemany(
                "INSERT OR REPLACE INTO releases "
                "(id, title, artist, artist_key, label, label_key, catno, year, country, format, genres, styles, master_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
        if style_rows:
            con.executemany(
                "INSERT INTO release_styles (release_id, style) VALUES (?,?)", style_rows)
            style_rows.clear()
        con.commit()

    with gzip.open(gz_path, "rb") as raw:
        stream = io.BufferedReader(_RootWrappedStream(raw, needs_wrap, skip_bytes))
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)          # capture la racine pour la vider au fil de l'eau
        for event, elem in context:
            if event != "end" or elem.tag != "release":
                continue
            n_total += 1
            parsed = _parse_release_elem(elem)
            elem.clear()
            root.clear()                 # `elem.clear()` seul ne suffit pas : la racine garde
                                          # une référence sur chaque enfant traité (fuite mémoire
                                          # sur 18-20M sorties sans ce clear-là aussi)
            if parsed is not None:
                row, styles_list = parsed
                n_vinyl += 1
                batch.append(row)
                style_rows.extend((row[0], s) for s in styles_list)
                if len(batch) >= batch_size:
                    flush()
            if progress_cb and n_total % 20000 == 0:
                progress_cb(n_total)
    flush()
    if progress_cb:
        progress_cb(n_total)
    _create_indexes(con)
    con.execute("ANALYZE")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for suffix in ("-wal", "-shm"):               # compagnons WAL du fichier temporaire
        try:
            os.remove(new_path + suffix)
        except OSError:
            pass
    os.replace(new_path, DB_PATH)
    return n_total, n_vinyl


def _parse_release_elem(elem):
    """(row_releases, styles_list) ou None si pas vinyle. À valider/ajuster
    contre un vrai fichier — écrit d'après la structure documentée des dumps
    Discogs (artists/artist, labels/label, formats/format/descriptions/description,
    genres/genre, styles/style, master_id) ; utilise findtext/attrib
    défensivement pour ne pas planter tout l'import sur une variation
    ponctuelle. `styles_list` : liste brute (avant jointure), pour peupler
    `release_styles` sans avoir à re-découper la chaîne jointe."""
    rid = elem.get("id")
    if not rid:
        return None

    fmt_names, fmt_desc_parts = [], []
    for fmt in elem.findall("./formats/format"):
        name = fmt.get("name") or ""
        if name:
            fmt_names.append(name)
        fmt_desc_parts.extend(d.text or "" for d in fmt.findall("./descriptions/description"))
        text_attr = fmt.get("text")
        if text_attr:
            fmt_desc_parts.append(text_attr)
    fmt_descriptions = ", ".join(p for p in fmt_desc_parts if p)
    if not _is_vinyl(fmt_names, fmt_descriptions):
        return None

    artists = []
    for a in elem.findall("./artists/artist"):
        name = (a.findtext("name") or "").strip()
        if name:
            artists.append(name)
    artist = ", ".join(artists)

    labels, catnos = [], []
    for lab in elem.findall("./labels/label"):
        name = lab.get("name") or ""
        if name:
            labels.append(name)
        catno = lab.get("catno") or ""
        if catno and catno.lower() != "none":
            catnos.append(catno)
    label = labels[0] if labels else None
    catno = catnos[0] if catnos else None

    genres = ", ".join(g.text for g in elem.findall("./genres/genre") if g.text)
    styles_list = [s.text for s in elem.findall("./styles/style") if s.text]
    styles = ", ".join(styles_list)

    year = None
    released = elem.findtext("released") or ""
    m = re.match(r"(\d{4})", released)
    if m:
        year = int(m.group(1))

    master_id = elem.findtext("master_id")
    master_id = int(master_id) if master_id and master_id.isdigit() else None
    if not master_id:                     # "0" = pas de master (dumps 2026) -> None
        master_id = None

    title = (elem.findtext("title") or "").strip() or None

    row = (
        int(rid), title, artist, normalize_label(artist) if artist else None,
        label, normalize_label(label) if label else None, catno, year,
        elem.findtext("country"), fmt_descriptions, genres, styles, master_id,
    )
    return row, styles_list


# --------------------------------------------------------------- lookup (lecture, utilisé par l'appli)

def available():
    return os.path.exists(DB_PATH)


def connect_readonly():
    """Connexion réutilisable pour plusieurs lookups (ex. tout le stock 12"
    d'un vendeur) sans rouvrir le fichier à chaque fois. None si pas encore
    importé — l'appelant doit alors se rabattre sur son propre repli."""
    return sqlite3.connect(DB_PATH) if available() else None


def lookup_release(release_id, con=None):
    """{genres, styles, label, artist, year, format} ou None — utilisé pour
    enrichir gratuitement les items d'inventaire vendeur (qui portent déjà le
    release_id) sans appel API supplémentaire. `con` : réutiliser une
    connexion ouverte via `connect_readonly()` pour des lookups en série."""
    owns = con is None
    if owns:
        con = connect_readonly()
        if con is None:
            return None
    try:
        row = con.execute(
            "SELECT title, artist, label, catno, year, country, format, genres, styles, master_id "
            "FROM releases WHERE id = ?", (int(release_id),)).fetchone()
    finally:
        if owns:
            con.close()
    if not row:
        return None
    keys = ("title", "artist", "label", "catno", "year", "country", "format", "genres", "styles", "master_id")
    return dict(zip(keys, row))


def search_by_label(label_name, limit=2000):
    if not available():
        return []
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT id, title, artist, year, format, genres, styles FROM releases "
            "WHERE label_key = ? LIMIT ?", (normalize_label(label_name), limit)).fetchall()
    finally:
        con.close()
    keys = ("id", "title", "artist", "year", "format", "genres", "styles")
    return [dict(zip(keys, r)) for r in rows]


def suggest_labels(prefix, limit=12):
    """Noms de labels du référentiel local dont la clé normalisée commence par
    `prefix` — typeahead d'ajout de label sans appel API Discogs. Sur-échantillonne
    puis déduplique en Python (une même clé normalisée peut avoir plusieurs graphies
    selon les sorties : casse, variantes).

    Bornes de plage plutôt que `LIKE 'prefix%'` : un `LIKE` sur une colonne
    insensible à la casse par défaut ne peut pas servir de borne d'index —
    `EXPLAIN QUERY PLAN` le confirme (SCAN, pas SEARCH, malgré l'index
    présent). `>= norm AND < norm + '\\uffff'` transforme la requête en une
    vraie recherche par index, sur une table de plusieurs millions de lignes."""
    if not available():
        return []
    norm = normalize_label(prefix)
    if not norm:
        return []
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT label, label_key FROM releases "
            "WHERE label_key >= ? AND label_key < ? AND label IS NOT NULL "
            "ORDER BY label_key LIMIT ?", (norm, norm + "￿", limit * 8)).fetchall()
    finally:
        con.close()
    seen, out = set(), []
    for label, lk in rows:
        if lk in seen:
            continue
        seen.add(lk)
        out.append(label)
        if len(out) >= limit:
            break
    return out
