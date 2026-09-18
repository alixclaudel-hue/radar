"""Playlist RECOS RADAR — vidéo attachée à la sortie Discogs avant la recherche
YouTube (demande utilisateur 2026-09-18).

`job_publish_recos` essaie d'abord la vidéo que Discogs associe déjà à la sortie
du candidat, comme le fait le bouton play de la tracklist de /search, et ne
retombe sur une recherche YouTube que si rien ne correspond. Enjeu : une
recherche coûte ~100 unités de quota (80 par jour au mieux, cf. points 47-53 de
CLAUDE.md), la vidéo Discogs n'en coûte aucune — seule sa vérification de
lisibilité coûte 1 unité sur `/videos`, métrique distincte.

Aucun accès réseau : `discogs_get` et les appels YouTube sont simulés, les
chemins de fichiers pointent vers un dossier temporaire (jamais le vrai /data).

Lancer : python3 -m unittest tests.test_recos_discogs_video -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crate_jobs  # noqa: E402
from radar_web.radar import textmatch, ytcache  # noqa: E402

from tests.test_job_publish_recos import FakeJob  # noqa: E402

VIDEOS = [
    {"uri": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Inland Knights - 12 Till 8"},
    {"uri": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "Craig De Sousa - Sunrise"},
]


class BestVideoUriTestCase(unittest.TestCase):
    def test_retient_la_video_de_la_piste_demandee(self):
        self.assertEqual(textmatch.best_video_uri(VIDEOS, "Inland Knights", "12 Till 8"),
                         "https://www.youtube.com/watch?v=aaaaaaaaaaa")

    def test_rien_quand_aucune_video_ne_correspond(self):
        self.assertEqual(textmatch.best_video_uri(VIDEOS, "Soichi Terada", "Sun Showers"), "")

    def test_liste_vide_ou_absente(self):
        self.assertEqual(textmatch.best_video_uri([], "A", "B"), "")
        self.assertEqual(textmatch.best_video_uri(None, "A", "B"), "")

    def test_entree_sans_uri_ignoree(self):
        vids = [{"title": "Inland Knights - 12 Till 8"}]      # pas d'uri : inexploitable
        self.assertEqual(textmatch.best_video_uri(vids, "Inland Knights", "12 Till 8"), "")

    def test_video_d_une_autre_piste_du_meme_artiste_rejetee(self):
        """Défaut trouvé en testant /search : « Inland Knights — Inconnue » passait
        le seuil GLOBAL face à la vidéo de « 12 Till 8 » (artiste commun, 2 jetons
        sur 3) et héritait de la vidéo d'une autre piste. Le titre de la piste doit
        être couvert à lui seul."""
        self.assertEqual(textmatch.best_video_uri(VIDEOS, "Inland Knights", "Inconnue"), "")

    def test_titre_vide_n_apparie_rien(self):
        self.assertEqual(textmatch.best_video_uri(VIDEOS, "Inland Knights", ""), "")

    def test_correspondance_partielle_sous_le_seuil_rejetee(self):
        """Une vidéo qui ne partage que l'artiste ne doit PAS être prise pour la
        piste : mauvaise vidéo pire que pas de vidéo (cf. VIDEO_MATCH_MIN)."""
        vids = [{"uri": "https://youtu.be/ccccccccccc", "title": "Inland Knights live at Panorama Bar"}]
        self.assertEqual(textmatch.best_video_uri(vids, "Inland Knights", "12 Till 8"), "")


class YoutubeIdTestCase(unittest.TestCase):
    def test_graphies_reconnues(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "http://youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ",
                    "https://www.youtube.com/embed/dQw4w9WgXcQ"):
            self.assertEqual(ytcache.youtube_id(url), "dQw4w9WgXcQ", url)

    def test_url_non_youtube(self):
        self.assertEqual(ytcache.youtube_id("https://vimeo.com/12345"), "")
        self.assertEqual(ytcache.youtube_id(""), "")
        self.assertEqual(ytcache.youtube_id(None), "")


class PlayableVideoTestCase(unittest.TestCase):
    def _resp(self, vid, upload="processed", privacy="public"):
        return {"items": [{"id": vid, "status": {"uploadStatus": upload,
                                                  "privacyStatus": privacy}}]}

    def test_video_publique(self):
        with mock.patch.object(ytcache, "request", return_value=self._resp("x")):
            self.assertTrue(ytcache.playable_video("x", ["k"]))

    def test_video_non_listee_acceptee(self):
        with mock.patch.object(ytcache, "request", return_value=self._resp("x", privacy="unlisted")):
            self.assertTrue(ytcache.playable_video("x", ["k"]))

    def test_video_privee_refusee(self):
        with mock.patch.object(ytcache, "request", return_value=self._resp("x", privacy="private")):
            self.assertFalse(ytcache.playable_video("x", ["k"]))

    def test_video_absente_de_la_reponse_refusee(self):
        """Vidéo supprimée : `/videos` répond 200 avec `items` vide."""
        with mock.patch.object(ytcache, "request", return_value={"items": []}):
            self.assertFalse(ytcache.playable_video("x", ["k"]))

    def test_erreur_api_non_classee_refuse(self):
        with mock.patch.object(ytcache, "request", side_effect=RuntimeError("403")):
            self.assertFalse(ytcache.playable_video("x", ["k"]))

    def test_quota_remonte_a_l_appelant(self):
        with mock.patch.object(ytcache, "request", side_effect=ytcache.QuotaExhausted("ko")):
            with self.assertRaises(ytcache.QuotaExhausted):
                ytcache.playable_video("x", ["k"])

    def test_identifiant_vide(self):
        with mock.patch.object(ytcache, "request", side_effect=AssertionError("aucun appel attendu")):
            self.assertFalse(ytcache.playable_video("", ["k"]))


class PublishRecosDiscogsFirstTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        self.paths = {
            "RECOS_CANDIDATES_PATH": os.path.join(tmp, "recos_candidates.json"),
            "RECOS_HISTORY_PATH": os.path.join(tmp, "recos_playlist_history.json"),
            "RECOS_PLAYLIST_PATH": os.path.join(tmp, "recos_playlist.json"),
            "RECOS_SEARCH_BUDGET_PATH": os.path.join(tmp, "recos_search_budget.json"),
        }
        patchers = [mock.patch.object(crate_jobs, name, path)
                    for name, path in self.paths.items()]
        patchers += [
            mock.patch.object(crate_jobs, "cfg_load",
                              return_value={"token": "tok",
                                            "scoring": {"recos": {"max_tracks": 10}}}),
            mock.patch.object(ytcache, "youtube_keys", return_value=["k"]),
            mock.patch.object(crate_jobs.time, "sleep"),   # cadence Discogs : pas d'attente en test
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _candidate(self, **kw):
        c = {"artist": "Inland Knights", "title": "12 Till 8", "label": "Drop Music",
             "release_id": 1228717}
        c.update(kw)
        return c

    def _playlist(self):
        return crate_jobs.load_json(self.paths["RECOS_PLAYLIST_PATH"], [])

    def _budget_count(self):
        d = crate_jobs.load_json(self.paths["RECOS_SEARCH_BUDGET_PATH"], {})
        return int(d.get("count", 0))

    def test_video_discogs_publiee_sans_recherche_youtube(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], [self._candidate()])
        with mock.patch.object(crate_jobs, "discogs_get", return_value={"videos": VIDEOS}), \
             mock.patch.object(ytcache, "playable_video", return_value=True), \
             mock.patch.object(ytcache, "search_video_diag",
                                side_effect=AssertionError("aucune recherche attendue")) as search:
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(search.call_count, 0)
        self.assertEqual([t["video_id"] for t in self._playlist()], ["aaaaaaaaaaa"])
        self.assertEqual(self._budget_count(), 0)          # aucune unité de recherche consommée
        self.assertIn("vidéo Discogs", job.ticks[-1])

    def test_repli_recherche_si_aucune_video_ne_correspond(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"],
                             [self._candidate(artist="Soichi Terada", title="Sun Showers")])
        with mock.patch.object(crate_jobs, "discogs_get", return_value={"videos": VIDEOS}), \
             mock.patch.object(ytcache, "search_video_diag", return_value=("zzz", "")) as search:
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(search.call_count, 1)
        self.assertEqual([t["video_id"] for t in self._playlist()], ["zzz"])
        self.assertEqual(self._budget_count(), 1)
        self.assertIn("recherche YouTube", job.ticks[-1])

    def test_repli_recherche_si_la_video_discogs_est_illisible(self):
        """Lien Discogs vers une vidéo supprimée/privée : ne jamais publier tel
        quel, repasser par la recherche."""
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], [self._candidate()])
        with mock.patch.object(crate_jobs, "discogs_get", return_value={"videos": VIDEOS}), \
             mock.patch.object(ytcache, "playable_video", return_value=False), \
             mock.patch.object(ytcache, "search_video_diag", return_value=("zzz", "")) as search:
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(search.call_count, 1)
        self.assertEqual([t["video_id"] for t in self._playlist()], ["zzz"])

    def test_sortie_sans_video_declenche_la_recherche(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], [self._candidate()])
        with mock.patch.object(crate_jobs, "discogs_get", return_value={}), \
             mock.patch.object(ytcache, "search_video_diag", return_value=("zzz", "")) as search:
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(search.call_count, 1)

    def test_publie_encore_quand_le_budget_du_jour_est_epuise(self):
        """Intérêt principal du chemin Discogs : il ne dépend pas du quota de
        recherche, donc la playlist continue de se remplir après épuisement."""
        crate_jobs._recos_searches_record(crate_jobs.RECOS_DAILY_SEARCH_BUDGET)
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], [self._candidate()])
        with mock.patch.object(crate_jobs, "discogs_get", return_value={"videos": VIDEOS}), \
             mock.patch.object(ytcache, "playable_video", return_value=True), \
             mock.patch.object(ytcache, "search_video_diag",
                                side_effect=AssertionError("aucune recherche attendue")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual([t["video_id"] for t in self._playlist()], ["aaaaaaaaaaa"])

    def test_budget_epuise_sans_token_discogs_arrete_le_run(self):
        """Sans token, aucun chemin ne contourne le quota : on garde l'arrêt net."""
        crate_jobs._recos_searches_record(crate_jobs.RECOS_DAILY_SEARCH_BUDGET)
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], [self._candidate()])
        with mock.patch.object(crate_jobs, "cfg_load", return_value={}), \
             mock.patch.object(crate_jobs, "discogs_get",
                                side_effect=AssertionError("aucun appel Discogs attendu")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertIn("Budget quotidien", job.finished)
        self.assertEqual(self._playlist(), [])

    def test_plafond_d_appels_discogs_par_lancement(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"],
                             [self._candidate(title=f"Track {i}") for i in range(4)])
        with mock.patch.object(crate_jobs, "RECOS_DISCOGS_LOOKUPS_PER_RUN", 2), \
             mock.patch.object(crate_jobs, "discogs_get", return_value={}) as dg, \
             mock.patch.object(ytcache, "search_video_diag", return_value=("", "rien")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(dg.call_count, 2)

    def test_candidat_sans_release_id_passe_direct_a_la_recherche(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"],
                             [self._candidate(release_id=None)])
        with mock.patch.object(crate_jobs, "discogs_get",
                                side_effect=AssertionError("aucun appel Discogs attendu")), \
             mock.patch.object(ytcache, "search_video_diag", return_value=("zzz", "")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual([t["video_id"] for t in self._playlist()], ["zzz"])


class SearchTracklistTestCase(unittest.TestCase):
    """Non-régression du bouton play de /search : la tracklist d'une sortie
    utilise désormais le MÊME helper que la playlist RECOS (`best_video_uri`)
    au lieu de sa propre boucle d'appariement."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from radar_web import app as appmod
        self.appmod = appmod
        p = mock.patch.object(appmod, "_dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def _release(self):
        return {
            "artists": [{"name": "Inland Knights"}],
            "labels": [{"name": "Drop Music"}],
            "year": 2000,
            "videos": VIDEOS,
            "tracklist": [
                {"position": "A1", "type_": "track", "title": "12 Till 8"},
                {"position": "B1", "type_": "track", "title": "Inconnue"},
            ],
        }

    def test_piste_avec_video_discogs_et_piste_sans(self):
        with mock.patch.object(self.appmod.discogs, "release", return_value=self._release()):
            r = self.client.get("/release/16036/tracks")
        self.assertEqual(r.status_code, 200)
        # piste appariée : lien direct vers la vidéo Discogs
        self.assertIn("https://www.youtube.com/watch?v=aaaaaaaaaaa", r.text)
        # piste non appariée : repli sur la recherche YouTube
        self.assertIn("/yt/first?q=", r.text)


if __name__ == "__main__":
    unittest.main()
