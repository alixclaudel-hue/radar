"""Mini gateway HTTP local pour Gemini — session cloud.

Contexte : la session cloud n'exporte pas `GEMINI_API_KEY` et l'identifiant
réseau du proxy n'autorise pas `generativelanguage.googleapis.com` — un appel
direct de `scripts/ai_query.py` échoue en 401. La clé, en revanche, est déjà
posée dans `~/.claude-code-router/config.sqlite` (par CCR). Ce gateway
écoute en loopback, lit cette clé au démarrage et ajoute `?key=<clé>` à
chaque requête forwardée vers Google. Le script client (`ai_query.py`)
reste inchangé — il pointe simplement `RADAR_GEMINI_BASE_URL` ici.

Sur le VPS (clé exportée par docker compose), ce gateway n'est pas démarré :
`ai_query.py` appelle directement Google, comme aujourd'hui.

Cf. CLAUDE.md pt 44 (chantier Gemini gateway) et skill ask-gemini.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM_HOST = "https://generativelanguage.googleapis.com"
PATH_PREFIX = "/v1beta/models/"
CCR_CONFIG_DB = Path.home() / ".claude-code-router" / "config.sqlite"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8632
UPSTREAM_TIMEOUT = 90.0  # Gemini thinking peut prendre ~60 s


def _key_from_ccr(db_path: Path = CCR_CONFIG_DB) -> str | None:
    """Lit la clé Gemini stockée par CCR dans sa base SQLite.

    Retourne `None` si la base n'existe pas, si la table est vide, si le JSON
    est invalide, ou si aucun provider `gemini` ne porte de clé — cette fonction
    ne lève jamais, l'appelant décide quoi faire en cas d'absence.
    """
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value_json FROM app_config WHERE key = 'default'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        cfg = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    for provider in cfg.get("Providers", []):
        if provider.get("name") == "gemini":
            key = (provider.get("api_key") or "").strip()
            if key:
                return key
    return None


def _resolve_key() -> str:
    """Résout la clé Gemini au démarrage — CCR prioritaire sur l'env.

    Ordre volontairement inversé de l'usage courant : dans une session cloud
    claude.ai, `GEMINI_API_KEY` peut être injectée par le proxy sortant sans
    être une clé Google valide (elle sert la « connexion Gemini API » interne
    du proxy et fait échouer les appels directs en 401 ACCESS_TOKEN_TYPE_
    UNSUPPORTED). La clé posée par l'utilisateur dans CCR est la seule dont
    l'origine est explicite : on la prend en priorité, l'env n'est qu'un
    repli pour l'usage hors session cloud (VPS, dev local).
    """
    ccr_key = _key_from_ccr()
    if ccr_key:
        return ccr_key
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_key:
        return env_key
    raise SystemExit(
        "gemini_gateway : aucune clé disponible "
        "(pas de provider 'gemini' avec api_key dans "
        f"{CCR_CONFIG_DB} ET GEMINI_API_KEY vide). Configurez CCR (ccr ui) "
        "ou exportez GEMINI_API_KEY."
    )


class GeminiProxyHandler(BaseHTTPRequestHandler):
    """Handler HTTP : n'accepte que POST sur `/v1beta/models/*:generateContent`.

    Toute autre méthode ou chemin renvoie 404 immédiat, pour minimiser la
    surface d'attaque en cas d'exposition accidentelle hors loopback.
    """

    server_version = "gemini-gateway/1.0"
    api_key: str = ""  # posé sur la classe par `main()` au démarrage

    def log_message(self, format: str, *args) -> None:
        # Silence les logs verbeux par défaut ; on trace nous-mêmes en fin de requête.
        return

    def _reject(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"code": status, "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        # Autorise seulement /health pour un smoke test trivial.
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._reject(404, "Only POST to /v1beta/models/*:generateContent is accepted.")

    def do_POST(self) -> None:  # noqa: N802
        # Double-check loopback : bind est déjà 127.0.0.1, mais on n'ouvre
        # rien à un routage IP surprenant.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._reject(403, "Loopback only.")
            return
        if not self.path.startswith(PATH_PREFIX):
            self._reject(404, f"Path must start with {PATH_PREFIX}.")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""

        upstream_url = f"{UPSTREAM_HOST}{self.path}"
        sep = "&" if "?" in upstream_url else "?"
        upstream_url = f"{upstream_url}{sep}key={self.api_key}"

        req = urllib.request.Request(
            upstream_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                upstream_body = resp.read()
                upstream_status = resp.status
                content_type = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            upstream_body = exc.read() or b""
            upstream_status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            body_err = json.dumps({
                "error": {"code": 502, "message": f"Upstream network error: {exc.__class__.__name__}"}
            }).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_err)))
            self.end_headers()
            self.wfile.write(body_err)
            self._trace("POST", self.path, 502, time.monotonic() - started)
            return

        self.send_response(upstream_status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(upstream_body)))
        self.end_headers()
        self.wfile.write(upstream_body)
        self._trace("POST", self.path, upstream_status, time.monotonic() - started)

    def _trace(self, method: str, path: str, status: int, elapsed: float) -> None:
        # On garde la trace sur stderr, sans body ni clé, pour lecture par tail -f.
        sys.stderr.write(
            f"[gemini-gateway] {method} {path} -> {status} ({elapsed:.2f}s)\n"
        )
        sys.stderr.flush()


def main() -> int:
    host = os.environ.get("RADAR_GEMINI_GATEWAY_HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("RADAR_GEMINI_GATEWAY_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT

    GeminiProxyHandler.api_key = _resolve_key()
    server = ThreadingHTTPServer((host, port), GeminiProxyHandler)

    def _shutdown(signum, frame):  # noqa: ARG001
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    sys.stderr.write(
        f"[gemini-gateway] écoute sur http://{host}:{port}{PATH_PREFIX} "
        f"(pointer RADAR_GEMINI_BASE_URL vers http://{host}:{port}/v1beta/models/)\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
