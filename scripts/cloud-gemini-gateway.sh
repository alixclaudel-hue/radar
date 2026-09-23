#!/bin/bash
# Démarre le mini gateway Gemini local (scripts/gemini_gateway.py) au démarrage
# d'une session cloud claude.ai — comme cloud-setup.sh, sans effet hors cloud.
#
# Contexte : dans une session cloud, `GEMINI_API_KEY` de l'env est injectée par
# le proxy sortant et échoue en 401 sur v1beta (ACCESS_TOKEN_TYPE_UNSUPPORTED).
# La clé utilisateur posée dans CCR (~/.claude-code-router/config.sqlite) est
# la seule qui s'authentifie. Le gateway l'ajoute en `?key=` (query, jamais
# header — le proxy réécrit x-goog-api-key), et publie son URL dans un
# marqueur éphémère lu par ai_query.py.
#
# Sur le VPS, ce script n'est jamais joué (CLAUDE_CODE_REMOTE≠true).
set -u

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

MARKER=/tmp/radar-gemini-gateway.url
PORT=${RADAR_GEMINI_GATEWAY_PORT:-8632}
URL="http://127.0.0.1:${PORT}/v1beta/models/"

# Déjà démarré et joignable : on ne relance rien.
if curl -sS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "$URL" > "$MARKER"
  exit 0
fi

# Pas de clé utilisateur dans CCR ? On ne démarre pas — la porte de sortie
# actuelle de la règle N°1 (un reçu d'échec débloque le workflow) prendra le
# relais, comme aujourd'hui.
KEY_PRESENT=$(python3 - <<'PY'
import json, sqlite3, sys
from pathlib import Path
db = Path.home() / ".claude-code-router" / "config.sqlite"
if not db.is_file():
    sys.exit(1)
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value_json FROM app_config WHERE key='default'").fetchone()
    con.close()
except Exception:
    sys.exit(1)
if not row or not row[0]:
    sys.exit(1)
try:
    cfg = json.loads(row[0])
except Exception:
    sys.exit(1)
for p in cfg.get("Providers", []):
    if p.get("name") == "gemini" and (p.get("api_key") or "").strip():
        sys.exit(0)
sys.exit(1)
PY
)
KEY_STATUS=$?
if [ "$KEY_STATUS" -ne 0 ]; then
  echo "· gemini-gateway : aucune clé dans CCR — porte de sortie règle N°1 (échec = reçu = déblocage) active."
  exit 0
fi

# Lance le gateway en fond, log dans /tmp.
nohup python3 "$CLAUDE_PROJECT_DIR/scripts/gemini_gateway.py" \
  >/tmp/radar-gemini-gateway.log 2>&1 &
GW_PID=$!

# Attend qu'il réponde (max 3 s).
for _ in 1 2 3 4 5 6; do
  sleep 0.5
  if curl -sS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "$URL" > "$MARKER"
    echo "· gemini-gateway : démarré (pid $GW_PID) → ai_query.py routera vers $URL"
    exit 0
  fi
done

echo "· gemini-gateway : n'a pas démarré à temps (pid $GW_PID, log /tmp/radar-gemini-gateway.log). Porte de sortie règle N°1 active."
exit 0
