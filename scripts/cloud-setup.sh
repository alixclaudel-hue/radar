#!/bin/bash
# Prépare une session Claude Code lancée DANS LE CLOUD (VM Ubuntu jetable, dépôt
# fraîchement cloné : pas de /data, pas de .env, pas de token Discogs).
# Sans effet en local : le garde ci-dessous sort immédiatement hors du cloud.
set -u

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

# Cache d'environnement encore chaud → dépendances déjà là, on ne refait rien.
if python -c "import fastapi, uvicorn, jinja2, multipart, requests" 2>/dev/null; then
  exit 0
fi

echo "· cloud-setup : installation de la stack web (FastAPI) pour le smoke test…"
# Mêmes dépendances minimales que la CI (.github/workflows/ci.yml) : ni playwright
# ni yt-dlp, inutiles pour éditer/tester les routes web.
pip install -q \
  "fastapi>=0.115" "uvicorn[standard]>=0.30" "jinja2>=3.1" \
  "python-multipart>=0.0.9" "requests>=2.28" || true

exit 0
