"""Retour utilisateur 2026-09-17 : RECOS RADAR proposait toutes les pistes de
« Asakusa Light » (Soichi Terada) alors que l'album est déjà dans la collection
Discogs de l'utilisateur. Élargi le 20/09 (issue #62) à la collection Bandcamp
(corpus, job_ingest_bandcamp) : même remarque, un achat Bandcamp n'est pas
moins « possédé » qu'un disque en collection Discogs.

Vérifie que `radar_jobs.recos.job_scan_recos` écarte les pistes d'un album possédé
(Discogs OU Bandcamp), par release_id ET par identité artiste+titre (un autre
pressage du même disque porte un id Discogs différent), sans écarter le reste.

Base scorestore SQLite synthétique dans un dossier temporaire, chemins de
`radar_jobs.recos` patchés : aucun accès au vrai `/data`, aucun appel réseau.

Lancer : python3 -m unittest tests.test_job_scan_recos_owned -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_jobs import ingest, recos, tracks  # noqa: E402
from radar_web.radar import scorestore  # noqa: E402

from tests.test_job_publish_recos import FakeJob  # noqa: E402


class ScanRecosOwnedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        self.paths = {
            "RECOS_CANDIDATES_PATH": os.path.join(tmp, "recos_candidates.json"),
            "RECOS_HISTORY_PATH": os.path.join(tmp, "recos_playlist_history.json"),
            "RECOS_PLAYLIST_PATH": os.path.join(tmp, "recos_playlist.json"),
            "COLLECTION_CACHE_PATH": os.path.join(tmp, "collection_cache.json"),
            "CORPUS_PATH": os.path.join(tmp, "taste_corpus.json"),
        }
        patchers = [mock.patch.object(recos, name, path)
                    for name, path in self.paths.items()]
        patchers.append(mock.patch.object(recos, "cfg_load", return_value={}))
        self.db = os.path.join(tmp, "scorestore.sqlite3")
        patchers.append(mock.patch.object(scorestore, "db_path", lambda uid: self.db))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self._seed_scores()

    def _seed_scores(self):
        """2 sorties notées, 1 piste chacune : l'album possédé (id 111) et un
        autre pressage du même album (id 222, même artiste+titre), plus une
        sortie jamais possédée (id 333)."""
        con = scorestore.open_db("owner")
        rows = [
            (111, "Soichi Terada", "Asakusa Light", "Bring Back The Rhythm"),
            (222, "Soichi Terada", "Asakusa Light", "Freaky Ride"),
            (333, "Inland Knights", "Big Audio Spidermite", "12 Till 8"),
        ]
        for rid, artist, rtitle, ttitle in rows:
            con.execute("INSERT INTO release_scores (release_id, label_key, label, artist, "
                        "title, year, styles, score, detail_json, computed_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, "label", "Label", artist, rtitle, 2022, "House", 80, "{}", ""))
            con.execute("INSERT INTO track_scores (release_id, track_no, artist, title, "
                        "score, detail_json, computed_at) VALUES (?,?,?,?,?,?,?)",
                        (rid, 1, artist, ttitle, 80, "{}", ""))
        con.commit()
        con.close()

    def _collection(self, **kw):
        recos.save_json(self.paths["COLLECTION_CACHE_PATH"], kw)

    def _corpus(self, rows):
        recos.save_json(self.paths["CORPUS_PATH"], rows)

    def _run(self):
        job = FakeJob()
        recos.job_scan_recos(job, {})
        return job, recos.load_json(self.paths["RECOS_CANDIDATES_PATH"], [])

    def test_sans_collection_rien_n_est_ecarte(self):
        job, candidates = self._run()
        self.assertEqual(sorted(c["release_id"] for c in candidates), [111, 222, 333])
        self.assertNotIn("collection", job.finished)

    def test_album_possede_ecarte_par_release_id_et_par_identite(self):
        # collection = le seul pressage 111 ; 222 est le MÊME album sous un autre
        # id Discogs, il doit tomber lui aussi (clé artiste+titre).
        self._collection(owned_release_ids=[111],
                         owned_release_keys=[tracks._release_identity_key(
                             "Soichi Terada", "Asakusa Light")])
        job, candidates = self._run()
        self.assertEqual([c["release_id"] for c in candidates], [333])
        self.assertIn("déjà en collection", job.finished)

    def test_identite_seule_suffit_quand_l_id_differe(self):
        # cas réel : la collection porte un pressage (id 999) que la découverte
        # RECOS n'a jamais vu — seule l'identité artiste+titre peut rapprocher.
        self._collection(owned_release_ids=[999],
                         owned_release_keys=[tracks._release_identity_key(
                             "Soichi Terada", "Asakusa Light")])
        _, candidates = self._run()
        self.assertEqual([c["release_id"] for c in candidates], [333])

    def test_album_bandcamp_ecarte_par_release_id_et_par_identite(self):
        # même logique que la collection Discogs, mais via le corpus Bandcamp
        # (job_ingest_bandcamp) : 111 possédé sur Bandcamp, 222 est le même
        # album sous un autre pressage/id Discogs -> écarté aussi par identité.
        self._corpus([{"source": "bandcamp", "artist": "Soichi Terada",
                      "title": "Asakusa Light", "release_id": 111}])
        job, candidates = self._run()
        self.assertEqual([c["release_id"] for c in candidates], [333])
        self.assertIn("déjà en collection", job.finished)

    def test_corpus_sans_bandcamp_n_ecarte_rien(self):
        # une entrée corpus d'une autre source (youtube, djset...) ne doit
        # jamais être traitée comme une possession.
        self._corpus([{"source": "youtube", "artist": "Soichi Terada",
                      "title": "Asakusa Light", "release_id": 111}])
        _, candidates = self._run()
        self.assertEqual(sorted(c["release_id"] for c in candidates), [111, 222, 333])

    def test_cache_collection_d_avant_le_correctif_reste_inoffensif(self):
        # collection_cache.json écrit par une version antérieure : ni
        # owned_release_ids ni owned_release_keys -> filtre sans effet.
        self._collection(n_collection=42, label_counts={"drop music": 3})
        _, candidates = self._run()
        self.assertEqual(len(candidates), 3)


class ReleaseIdentityKeyTestCase(unittest.TestCase):
    def test_suffixe_de_desambiguisation_retire_sur_l_artiste_seulement(self):
        self.assertEqual(tracks._release_identity_key("Rhythm (2)", "Asakusa Light"),
                         tracks._release_identity_key("Rhythm", "Asakusa Light"))
        # un titre peut légitimement finir par un nombre entre parenthèses :
        # il ne doit pas être confondu avec un autre disque.
        self.assertNotEqual(tracks._release_identity_key("A", "Volume (2)"),
                            tracks._release_identity_key("A", "Volume"))

    def test_moitie_manquante_ne_produit_aucune_cle(self):
        self.assertEqual(tracks._release_identity_key("Soichi Terada", ""), "")
        self.assertEqual(tracks._release_identity_key("", "Asakusa Light"), "")
        self.assertEqual(tracks._release_identity_key(None, None), "")


class FetchCollectionOwnedTestCase(unittest.TestCase):
    """`job_fetch_collection` doit désormais mémoriser l'identité des disques
    possédés (ids + clés artiste+titre), source du filtre ci-dessus."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "collection_cache.json")
        patchers = [
            mock.patch.object(ingest, "COLLECTION_CACHE_PATH", self.cache),
            mock.patch.object(ingest, "cfg_load", return_value={"token": "t"}),
            mock.patch.object(ingest, "time", mock.Mock(sleep=lambda s: None)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _api(self, token, path, params=None):
        if path == "/oauth/identity":
            return {"username": "owner"}
        if "collection" in path:
            return {"releases": [{"id": 111, "basic_information": {
                "id": 111, "title": "Asakusa Light",
                "artists": [{"name": "Soichi Terada"}],
                "labels": [{"name": "Rush Hour", "id": 7}]}}],
                    "pagination": {"pages": 1}}
        return {"wants": [{"id": 555, "basic_information": {
            "id": 555, "title": "Un disque juste voulu",
            "artists": [{"name": "Autre"}], "labels": []}}],
                "pagination": {"pages": 1}}

    def test_collection_memorisee_wantlist_ignoree(self):
        with mock.patch.object(ingest, "discogs_get", side_effect=self._api):
            ingest.job_fetch_collection(FakeJob(), {"merge_base": False})
        cache = json.load(open(self.cache))
        self.assertEqual(cache["owned_release_ids"], [111])
        self.assertEqual(cache["owned_release_keys"],
                         [tracks._release_identity_key("Soichi Terada", "Asakusa Light")])
        # la wantlist n'est PAS de la possession : un disque voulu doit rester
        # proposable en reco.
        self.assertNotIn(555, cache["owned_release_ids"])


if __name__ == "__main__":
    unittest.main()
