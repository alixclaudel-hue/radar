import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import unittest
from radar_web.radar import discogs_dump as dd


class TestReleasesForArtists(unittest.TestCase):

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        dd._create_schema(self.con)

    def tearDown(self):
        self.con.close()

    def test_cles_exactes_dictionnaire(self):
        """Vérifie que les dictionnaires renvoyés possèdent exactement les clés attendues."""
        self.con.execute("INSERT INTO releases (id, title, artist, label, catno, year, genres, styles, is_vinyl) VALUES (1, 'Title', 'Artist', 'Label', 'Cat', 2020, 'Genre', 'Style', 1)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (1, 10, 'Primary')")
        self.con.commit()

        res = dd.releases_for_artists([10], con=self.con)
        self.assertEqual(len(res), 1)
        expected_keys = {"id", "title", "artist", "label", "catno", "year", "genres", "styles", "artist_id"}
        self.assertEqual(set(res[0].keys()), expected_keys)

    def test_tous_formats_inclus(self):
        """Vérifie qu'une sortie avec is_vinyl=0 est bien renvoyée (aucun filtre de format)."""
        self.con.execute("INSERT INTO releases (id, title, artist, label, catno, year, is_vinyl) VALUES (2, 'CD Release', 'Art', 'Lab', 'C2', 2019, 0)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (2, 20, 'Artist')")
        self.con.commit()

        res = dd.releases_for_artists([20], con=self.con)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], 2)

    def test_per_artist_limite_et_tri(self):
        """Vérifie que per_artist=2 sur un artiste à 4 sorties renvoie les 2 plus récentes (year DESC)."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (101, 'R1', 'A', 2010)")
        self.con.execute(
            "INSERT INTO releases (id, title, artist, year) VALUES (102, 'R2', 'A', 2022)"
        )
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (103, 'R3', 'A', 2015)")
        self.con.execute(
            "INSERT INTO releases (id, title, artist, year) VALUES (104, 'R4', 'A', 2023)"
        )
        for rid in [101, 102, 103, 104]:
            self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (?, 30, 'Member')", (rid,))
        self.con.commit()

        res = dd.releases_for_artists([30], per_artist=2, con=self.con)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["id"], 104)
        self.assertEqual(res[1]["id"], 102)

    def test_annee_null_classee_apres(self):
        """Vérifie qu'une année NULL est classée après les années connues."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (201, 'Known', 'A', 2020)")
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (202, 'Unknown', 'A', NULL)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (201, 40, 'Artist')")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (202, 40, 'Artist')")
        self.con.commit()

        res = dd.releases_for_artists([40], per_artist=1, con=self.con)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], 201)

    def test_roles_multiples_deduplication(self):
        """Vérifie qu'une sortie où l'artiste a 2 rôles n'apparaît qu'une seule fois."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (301, 'Double', 'A', 2021)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (301, 50, 'Guitar')")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (301, 50, 'Vocals')")
        self.con.commit()

        res = dd.releases_for_artists([50], con=self.con)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], 301)

    def test_ordre_deux_artistes(self):
        """Vérifie que les résultats pour deux artistes sont groupés dans l'ordre des IDs fournis."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (401, 'Rel1', 'A1', 2020)")
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (402, 'Rel2', 'A2', 2020)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (401, 61, 'Artist')")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (402, 60, 'Artist')")
        self.con.commit()

        res = dd.releases_for_artists([60, 61], con=self.con)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["artist_id"], 60)
        self.assertEqual(res[1]["artist_id"], 61)

    def test_nettoyage_et_deduplication_ids(self):
        """Vérifie le dédoublonnage, l'acceptation de chaînes numériques et l'ignorance des valeurs vides/non numériques."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (501, 'R1', 'A', 2020)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (501, 70, 'Artist')")
        self.con.commit()

        inputs = [70, " 70 ", "70", "abc", "", None, True, 70]
        res = dd.releases_for_artists(inputs, con=self.con)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["artist_id"], 70)

    def test_liste_vide_renvoie_liste_vide(self):
        """Vérifie qu'une liste vide d'IDs renvoie une liste vide."""
        res = dd.releases_for_artists([], con=self.con)
        self.assertEqual(res, [])

    def test_plus_de_400_ids(self):
        """Vérifie qu'un traitement de plus de 400 IDs (ex: 950) ne lève pas d'erreur SQL et retrouve la sortie."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (999, 'BigBatch', 'A', 2020)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (999, 9999, 'Artist')")
        self.con.commit()

        ids = list(range(1, 950)) + [9999]
        res = dd.releases_for_artists(ids, con=self.con)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], 999)

    def test_table_release_artists_absente(self):
        """Vérifie qu'une base sans la table release_artists renvoie une liste vide."""
        empty_con = sqlite3.connect(":memory:")
        empty_con.execute("CREATE TABLE releases (id INT)")
        res = dd.releases_for_artists([1, 2], con=empty_con)
        self.assertEqual(res, [])
        empty_con.close()

    def test_connexion_externe_non_fermee(self):
        """Vérifie que la connexion externe passée en paramètre n'est pas fermée par la fonction."""
        self.con.execute("INSERT INTO releases (id, title, artist, year) VALUES (888, 'Test', 'A', 2020)")
        self.con.execute("INSERT INTO release_artists (release_id, artist_id, role) VALUES (888, 88, 'Artist')")
        self.con.commit()

        dd.releases_for_artists([88], con=self.con)
        cursor = self.con.execute("SELECT COUNT(*) FROM releases")
        self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
