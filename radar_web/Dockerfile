# Image légère : FastAPI + uvicorn. (Les jobs Playwright tournent via crate_jobs.py ;
# si tu veux le scraping DJ sets côté conteneur, repars de l'image playwright/python.)
FROM python:3.12-slim

WORKDIR /app
COPY radar_web/requirements.txt radar_web/requirements.txt
RUN pip install --no-cache-dir -r radar_web/requirements.txt

COPY . .

ENV CRATE_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8600
CMD ["uvicorn", "radar_web.app:app", "--host", "0.0.0.0", "--port", "8600"]
