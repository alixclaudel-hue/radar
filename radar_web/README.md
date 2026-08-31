# Radar — interface web (FastAPI + HTMX)

Réécriture progressive de l'appli Streamlit. **Les deux tournent en parallèle** et
partagent les mêmes fichiers de données (`CRATE_DATA_DIR`, défaut = racine du repo).

## Lancer en local

```bash
pip install -r radar_web/requirements.txt
# depuis la racine du repo :
uvicorn radar_web.app:app --reload --port 8600
```

→ http://localhost:8600 (mot de passe seulement si `APP_PASSWORD` est défini).

## État du portage

| Section | Statut |
|---|---|
| 🔍 Recherche | ✅ (label ou filtres → résultats notés, tracklist à la demande) |
| 🎛️ Réglages, 🎤 Artistes, 📻 Veille, 🏪 Vendeurs, 🏷️ Labels, 🎧 Sources, 🎚️ Sets | ⏳ stubs — encore dans Streamlit |
| Auth | ✅ cookie signé 5 h glissantes |
| Jobs de fond | ✅ réutilise `crate_jobs.py` ; suivi via `/jobs/{name}/status` (polling HTMX) |

## Architecture

```
radar_web/
  app.py              routes FastAPI
  radar/
    paths.py          emplacements des JSON (partagés Streamlit + jobs)
    store.py          IO JSON, config, DEFAULT_SCORING, migrations
    discogs.py        client API Discogs
    scoring.py        Ctx + chaîne de notation (label/style complets ; artiste v0 sans graphe)
    jobs.py           lance/observe crate_jobs.py
  templates/          Jinja2 (base + pages + partials HTMX)
  static/app.css      thème « Sleeve »
```

La logique lourde (`crate_jobs.py`) est réutilisée telle quelle.

## Docker

```bash
docker build -f radar_web/Dockerfile -t radar-web .
docker run -p 8600:8600 -v "$PWD/data:/data" --env-file .env radar-web
```
