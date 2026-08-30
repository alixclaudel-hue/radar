# Image officielle Playwright : Python + Chromium + toutes les dépendances système
# déjà installées (indispensable pour l'extraction des DJ sets).
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Les données (config, base de labels, corpus, graphe, jobs…) vivent sur un volume
# monté ici — jamais dans l'image ni dans git.
ENV CRATE_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8501

CMD ["streamlit", "run", "crate_radar.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
