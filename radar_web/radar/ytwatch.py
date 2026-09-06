"""Détection d'écoute pour RECOS RADAR (lot 3) — retire de la playlist les pistes déjà
regardées, en lisant l'historique de visionnage YouTube. Aucune API officielle pour ça
(contrairement à la playlist elle-même, cf. radar/ytwrite.py) : scraping Playwright de
`/feed/history`.

Rejoue une session déjà authentifiée (`storage_state.json` Playwright — cookies +
stockage local) plutôt que d'automatiser un login Google : un login scripté est fragile
(détection anti-bot, contraire à l'esprit du service) et hors sujet ici — on a juste
besoin de LIRE une page déjà accessible à l'utilisateur connecté. Le fichier est exporté
UNE FOIS par l'utilisateur, avec un vrai navigateur sur sa machine (script fourni à côté,
cf. Mes sources → RECOS RADAR), puis importé et stocké par utilisateur — jamais un
profil navigateur partagé.

⚠️ Pas de garantie de stabilité : `/feed/history` n'est pas une page publique
documentée, ses sélecteurs peuvent changer sans préavis (même limite que la
marketplace Discogs dans ce projet, cf. CLAUDE.md § Pièges connus — en moins bloquant
ici puisqu'un échec dégrade seulement ce nettoyage, sans casser scan_recos/publish_recos
qui restent des jobs séparés). Non validé en conditions réelles au moment de l'écriture
(pas de compte Google ni de session accessible depuis la session de dev cloud)."""
import os
import re

from . import paths

HISTORY_URL = "https://www.youtube.com/feed/history"
_VIDEO_ID_RE = re.compile(r"(?:^|[?&])v=([A-Za-z0-9_-]{11})(?:&|$)")
_SIGNIN_RE = re.compile(r"sign in|se connecter", re.I)


class WatchSessionError(RuntimeError):
    """Aucune session importée, ou session expirée/déconnectée — à réimporter à la
    main (storage_state.json), jamais une reconnexion automatique."""


def session_path(uid):
    return paths.user_paths(uid).youtube_watch_state


def has_session(uid):
    return os.path.isfile(session_path(uid))


def save_session(uid, raw_bytes):
    p = session_path(uid)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw_bytes)
    os.replace(tmp, p)


def clear_session(uid):
    try:
        os.remove(session_path(uid))
    except OSError:
        pass


def extract_video_ids(hrefs):
    """Fonction pure — ids de vidéo trouvés dans une liste de hrefs `/watch?v=...`,
    dédupliqués, ordre de première apparition conservé. Séparée de Playwright
    (cf. fetch_watched_video_ids) pour rester testable sans navigateur ni réseau."""
    out, seen = [], set()
    for h in hrefs or []:
        m = _VIDEO_ID_RE.search(h or "")
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def fetch_watched_video_ids(uid, max_scrolls=8, scroll_pause_ms=600):
    """SEUL point d'entrée réseau de ce module (Playwright, headless) : charge la
    session sauvegardée, ouvre /feed/history, scrolle pour charger l'historique
    récent (chargement infini côté YouTube), renvoie les ids de vidéo trouvés
    (cf. extract_video_ids). Lève WatchSessionError si la session n'existe pas ou
    semble déconnectée (redirection vers la connexion Google détectée)."""
    if not has_session(uid):
        raise WatchSessionError("Aucune session de visionnage importée.")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=session_path(uid))
            page = context.new_page()
            page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=30000)
            if "accounts.google.com" in page.url or page.locator("a", has_text=_SIGNIN_RE).count():
                raise WatchSessionError(
                    "Session YouTube expirée ou déconnectée — réimporte storage_state.json.")
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(scroll_pause_ms)
            hrefs = page.eval_on_selector_all(
                "a#video-title, a#thumbnail", "els => els.map(e => e.getAttribute('href'))")
            return extract_video_ids(hrefs)
        finally:
            browser.close()
