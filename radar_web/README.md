# Radar — interface web (FastAPI + HTMX)

Interface active du projet (voir `CLAUDE.md` à la racine pour le contexte général).
L'ancienne appli Streamlit est retirée et gelée dans `archive/`.

## Lancer en local

```bash
pip install -r radar_web/requirements.txt
export CRATE_DATA_DIR=$PWD/data RADAR_NO_AUTH=1
# depuis la racine du repo :
uvicorn radar_web.app:app --reload --port 8600
```

→ http://localhost:8600. Sans `RADAR_NO_AUTH=1`, un login est exigé même sans compte.

## Architecture

```
radar_web/
  app.py              routes FastAPI
  worker.py           file de jobs (lance crate_jobs.py)
  radar/
    paths.py          emplacements des JSON
    store.py          IO JSON, config, DEFAULT_SCORING, migrations
    discogs.py        client API Discogs
    discogs_dump.py   index SQLite du dump mensuel (référentiel local)
    scoring.py        Ctx + chaîne de notation (le « cerveau »)
  templates/          Jinja2 (base + pages + partials HTMX)
  static/app.css      thème « Sleeve »
```

La logique des tâches longues (`crate_jobs.py`, à la racine) est réutilisée telle quelle.

## Docker

```bash
docker build -f radar_web/Dockerfile -t radar-web .
docker run -p 8600:8600 -v "$PWD/data:/data" --env-file .env radar-web
```
