"""Retour utilisateur 2026-09-17 : depuis RECOS RADAR, le bouton wantlist d'une
piste découverte sur une compilation CD ne trouvait aucun vinyle alors qu'un
pressage vinyle existe.

Cause : `app.py::cart_add` cherchait les pressages vinyle avec le titre de la
SORTIE (`title`), alors que la recherche Discogs filtre sur le champ `track`.
Cas réel : piste « 12 Till 8 » (Inland Knights) sur la compilation CD « Drop
Music » (USM Records) — artiste + track="Drop Music" ne renvoie rien, alors que
track="12 Till 8" retrouve le 12" Drop Music DRM011.

Aucun appel réseau : `_vinyl_matches` et le référentiel local sont simulés.

Lancer : python3 -m unittest tests.test_cart_add_vinyl_fallback -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `radar_web.app` lit les chemins de données à l'import : le dossier temporaire
# doit être posé AVANT, et rester valide tant que le processus de test vit (pas
# de nettoyage en tearDownClass, d'autres modules de test importés ensuite
# hériteraient d'un dossier supprimé).
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ["CRATE_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import discogs_dump as dd  # noqa: E402


class CartAddVinylFallbackTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app)
        self.calls = []
        patchers = [
            # pas de compte/cookie dans un test : le mode dev du middleware
            # d'authentification résout l'utilisateur par défaut.
            mock.patch.object(appmod, "_dev_mode", return_value=True),
            mock.patch.object(appmod, "_cfg", return_value={"token": "t"}),
            mock.patch.object(dd, "available", return_value=True),
            # sortie non-vinyle -> repli sur la recherche de pressages vinyle
            mock.patch.object(dd, "lookup_release", return_value={"is_vinyl": 0}),
            mock.patch.object(appmod, "_vinyl_matches",
                              side_effect=lambda token, a, t, **kw: self.calls.append((a, t)) or []),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_le_titre_de_la_piste_sert_a_chercher_le_vinyle(self):
        r = self.client.post("/cart/add", data={
            "rid": "1228717", "title": "Drop Music", "artist": "Inland Knights",
            "track": "12 Till 8", "label": "USM Records"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls, [("Inland Knights", "12 Till 8")])

    def test_repli_sur_le_titre_de_la_sortie_quand_aucune_piste_n_est_fournie(self):
        # /search et les autres écrans postent sans champ `track` : comportement
        # inchangé pour eux.
        r = self.client.post("/cart/add", data={
            "rid": "16036", "title": "Big Audio Spidermite", "artist": "Inland Knights"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls, [("Inland Knights", "Big Audio Spidermite")])


if __name__ == "__main__":
    unittest.main()
