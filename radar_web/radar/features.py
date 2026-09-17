"""Interrupteurs des fonctionnalités mises en pause.

« En pause » ne veut pas dire supprimée : le code, les gabarits, les jobs et
les données restent en place. Seuls les points d'entrée sont coupés (entrée de
nav, routes, jobs lançables, boucles automatiques du worker). Remettre un
drapeau à True puis redémarrer l'appli et le worker suffit à rendre la
fonctionnalité — rien d'autre à défaire.

Module SÉPARÉ, et pas deux constantes dans `app.py` : `radar_web/worker.py`
tourne dans son propre processus (service compose `radar-worker`) et n'importe
jamais `app.py`. Sans source commune, une fonctionnalité coupée côté web
continuerait d'être enfilée par le worker.
"""

# « Nouveautés » (/veille) : règles de veille + file des nouveautés vendeurs.
# En pause depuis le 2026-09-17 (décision utilisateur : pas utilisée pour
# l'instant, gardée pour plus tard). Jobs concernés : scan_veille, scan_sellers.
VEILLE_ENABLED = False

# Vendeurs : catalogue partagé de vendeurs Discogs (Réglages), scan hebdo de
# leurs inventaires et bloc « regrouper chez un vendeur » de la wantlist.
# En pause depuis le 2026-09-17 (décision utilisateur : le regroupement par
# vendeur se fait mieux directement sur Discogs). Job concerné : scan_catalog —
# sa boucle hebdo (`worker._maybe_weekly_scan`) ne s'arme plus, que
# RADAR_SELLER_SCAN=1 soit posé ou non dans l'environnement.
SELLERS_ENABLED = False
