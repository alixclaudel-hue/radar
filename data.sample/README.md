# data.sample

Jeu de données minimal pour démarrer en local sans rien ingérer.

```
cp -r data.sample data          # ou :  export CRATE_DATA_DIR="$PWD/data.sample"
cd radar_web && python -m uvicorn radar_web.app:app --reload --port 8600
```

Contient juste un `crate_radar_config.json` (quelques labels, deux catégories de
goût). Tout le reste (corpus, graphe, profils…) se construit via les jobs une fois
ton token Discogs saisi dans l'appli. Ces fichiers de données réels ne sont **pas**
versionnés (cf. `.gitignore`).
