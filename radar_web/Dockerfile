# Même base que la version Streamlit : Python + Chromium + deps système déjà là.
# Nécessaire pour que radar-web puisse lancer TOUS les jobs (dont l'extraction des
# DJ sets, qui utilise Playwright + yt-dlp via crate_jobs.py).
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY radar_web/requirements.txt radar_web/requirements.txt
RUN pip install --no-cache-dir -r radar_web/requirements.txt

COPY . .

ENV CRATE_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8600
# --forwarded-allow-ips : derrière Caddy (réseau Docker, jamais 127.0.0.1), sans ça
# uvicorn ignore X-Forwarded-Proto et l'appli se croit en http alors que le navigateur
# est en https -> request.base_url ne correspond plus à l'en-tête Origin, et _bad_origin
# rejette TOUS les POST du domaine public en 403 (constaté sur /account/invite).
CMD ["uvicorn", "radar_web.app:app", "--host", "0.0.0.0", "--port", "8600", \
     "--forwarded-allow-ips", "*"]
