'''Tests de la diversification des recos : quota par artiste et par label.

Vérifie que `radar_jobs.recos.job_scan_recos` limite à 2 le nombre de pistes
d'un même artiste et à 3 le nombre de pistes d'un même label dans
`recos_candidates.json`, que chaque candidat porte une clé `why` parmi
artist/label/style, et que le message final mentionne « quota artiste/label ».

Base scorestore SQLite synthétique dans un dossier temporaire, chemins de
`radar_jobs.recos` patchés : aucun accès réseau, aucun accès au vrai /data.

Lancer : python3 -m unittest tests.test_job_scan_recos_diversity -v
'''
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_jobs import recos  # noqa: E402
from radar_web.radar import scorestore  # noqa: E402

from tests.test_job_publish_recos import FakeJob  # noqa: E402


class ScanRecosDiversityTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        self.paths = {
            'RECOS_CANDIDATES_PATH': os.path.join(tmp, 'recos_candidates.json'),
            'RECOS_HISTORY_PATH': os.path.join(tmp, 'recos_playlist_history.json'),
            'RECOS_PLAYLIST_PATH': os.path.join(tmp, 'recos_playlist.json'),
            'COLLECTION_CACHE_PATH': os.path.join(tmp, 'collection_cache.json'),
            'CORPUS_PATH': os.path.join(tmp, 'taste_corpus.json'),
            'CART_PATH': os.path.join(tmp, 'cart.json'),
        }
        patchers = [mock.patch.object(recos, name, path)
                    for name, path in self.paths.items()]
        patchers.append(mock.patch.object(recos, 'cfg_load', return_value={}))
        self.db = os.path.join(tmp, 'scorestore.sqlite3')
        patchers.append(mock.patch.object(scorestore, 'db_path', lambda uid: self.db))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self._seed_scores()

    def _seed_scores(self, rows=None):
        '''Insère des sorties notées dans le scorestore synthétique.

        Par défaut : 5 pistes du même artiste (scores décroissants) puis
        2 pistes d'artistes différents. Surchargeable pour les cas limites.

        Chaque ligne est un tuple (release_id, label_key, label, artist,
        titre_sortie, titre_piste, score), avec un 8e élément optionnel
        `detail_json`. Sans précision, un detail_json non vide est utilisé
        (artiste 80 > label 50 > style 30) pour que les candidats exposent
        why == 'artist'.
        '''
        detail_par_defaut = json.dumps({'artist': 80, 'label': 50, 'style': 30})
        if rows is None:
            rows = [
                # (release_id, label_key, label, artist, titre_sortie,
                #  titre_piste, score)
                (111, 'lbl-a', 'Label A', 'Soichi Terada', 'Asakusa Light',
                 'Bring Back The Rhythm', 100),
                (112, 'lbl-b', 'Label B', 'Soichi Terada', 'Asakusa Light',
                 'Freaky Ride', 95),
                (113, 'lbl-c', 'Label C', 'Soichi Terada', 'Asakusa Light',
                 'Chikyu', 90),
                (114, 'lbl-d', 'Label D', 'Soichi Terada', 'Asakusa Light',
                 'Sun Shower', 85),
                (115, 'lbl-e', 'Label E', 'Soichi Terada', 'Asakusa Light',
                 'Distant Voices', 80),
                (333, 'lbl-f', 'Label F', 'Inland Knights',
                 'Big Audio Spidermite', '12 Till 8', 70),
                (444, 'lbl-g', 'Label G', 'DJ Sneak', 'Blue Funk',
                 'Deep', 60),
            ]
        con = scorestore.open_db('owner')
        con.execute('DELETE FROM release_scores')
        con.execute('DELETE FROM track_scores')
        for row in rows:
            rid, label_key, label, artist, rtitle, ttitle, score = row[:7]
            detail = row[7] if len(row) > 7 else detail_par_defaut
            con.execute(
                'INSERT INTO release_scores (release_id, label_key, label, artist, '
                'title, year, styles, score, detail_json, computed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (rid, label_key, label, artist, rtitle, 2022, 'House', score,
                 detail, ''))
            con.execute(
                'INSERT INTO track_scores (release_id, track_no, artist, title, '
                'score, detail_json, computed_at) VALUES (?,?,?,?,?,?,?)',
                (rid, 1, artist, ttitle, score, detail, ''))
        con.commit()
        con.close()

    def _run(self):
        job = FakeJob()
        recos.job_scan_recos(job, {})
        return job, recos.load_json(self.paths['RECOS_CANDIDATES_PATH'], [])

    def test_quota_artiste_limite_a_deux_pistes_et_conserve_les_autres(self):
        '''Cas nominal : 5 pistes du même artiste en tête, 2 d'autres artistes.
        Seules les 2 meilleures du premier artiste entrent dans les candidats.
        '''
        job, candidates = self._run()
        recos_ids = sorted(c['release_id'] for c in candidates)
        self.assertEqual(recos_ids, [111, 112, 333, 444])
        # Les 3 autres pistes du même artiste sont écartées.
        for rid in (113, 114, 115):
            self.assertNotIn(rid, recos_ids)
        # Message final mentionnant le quota.
        self.assertIn('quota artiste/label', job.finished)

    def test_chaque_candidat_porte_une_cle_why_autorisee(self):
        '''Chaque candidat doit exposer une clé why parmi artist/label/style.

        Les fixtures portent un detail_json non vide où la dimension artiste
        domine (80 > 50 > 30) : why vaut donc 'artist'.
        '''
        _, candidates = self._run()
        self.assertTrue(candidates)
        for c in candidates:
            self.assertIn('why', c)
            self.assertIn(c['why'], {'artist', 'label', 'style'})
            self.assertEqual(c['why'], 'artist')

    def test_quota_label_limite_a_trois_pistes(self):
        '''5 pistes du même label mais d'artistes différents -> 3 retenues.
        '''
        rows = [
            (201, 'lbl-x', 'Label X', 'Artiste 1', 'Titre 1', 'Piste 1', 100),
            (202, 'lbl-x', 'Label X', 'Artiste 2', 'Titre 2', 'Piste 2', 95),
            (203, 'lbl-x', 'Label X', 'Artiste 3', 'Titre 3', 'Piste 3', 90),
            (204, 'lbl-x', 'Label X', 'Artiste 4', 'Titre 4', 'Piste 4', 85),
            (205, 'lbl-x', 'Label X', 'Artiste 5', 'Titre 5', 'Piste 5', 80),
            (301, 'lbl-y', 'Label Y', 'Artiste 6', 'Titre 6', 'Piste 6', 70),
        ]
        self._seed_scores(rows)
        job, candidates = self._run()
        recos_ids = sorted(c['release_id'] for c in candidates)
        self.assertEqual(recos_ids, [201, 202, 203, 301])
        self.assertIn('quota artiste/label', job.finished)

    def test_scorestore_vide_ne_plante_pas_et_ne_produit_aucun_candidat(self):
        '''Liste vide : aucun candidat, pas de message de quota.
        '''
        self._seed_scores([])
        job, candidates = self._run()
        self.assertEqual(candidates, [])
        self.assertNotIn('quota artiste/label', job.finished)

    def test_pistes_a_artiste_vide_exclues_par_la_requete_sql(self):
        '''3 pistes à artiste vide et score >= 60 : la requête SQL les écarte
        (TRIM(artist) <> ''), aucun candidat ne doit donc être produit et le
        quota artiste/label n'est jamais atteint.
        '''
        rows = [
            (601, 'lbl-v1', 'Label V1', '', 'Album 1', 'Piste 1', 90),
            (602, 'lbl-v2', 'Label V2', '', 'Album 2', 'Piste 2', 80),
            (603, 'lbl-v3', 'Label V3', '', 'Album 3', 'Piste 3', 70),
        ]
        self._seed_scores(rows)
        job, candidates = self._run()
        self.assertEqual(candidates, [])
        self.assertNotIn('quota artiste/label', job.finished)

    def test_message_quota_absent_quand_aucun_depassement(self):
        '''Pas de dépassement -> le message de quota ne doit pas apparaître.
        '''
        rows = [
            (501, 'lbl-1', 'Label 1', 'Artiste 1', 'Titre 1', 'Piste 1', 100),
            (502, 'lbl-2', 'Label 2', 'Artiste 2', 'Titre 2', 'Piste 2', 90),
        ]
        self._seed_scores(rows)
        job, candidates = self._run()
        self.assertEqual(len(candidates), 2)
        self.assertNotIn('quota artiste/label', job.finished)


if __name__ == '__main__':
    unittest.main()
