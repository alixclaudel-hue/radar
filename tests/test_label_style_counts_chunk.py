"""`discogs_dump.label_style_counts` -- correctif B1 (brief VPS 18/09).

Avant ce correctif, la fonction construisait un unique `IN (...)` avec autant
de placeholders que de clés reçues. Au-delà de SQLITE_MAX_VARIABLE_NUMBER
(999 par défaut), SQLite levait `OperationalError: too many SQL variables`,
que le `except` du corps convertissait en dict vide -- indistinguable d'un
« aucun label n'a de styles ». `Ctx.label_affinities` appelant la fonction avec
la totalité des clés de `reco_rows` (465 284 mesurées en prod), le terme
`affinity` disparaissait en silence pour la quasi-totalité de l'univers classé.

Le plafond réel dépend de la build SQLite (32 766 depuis SQLite 3.32, mais
250 000 sur le CPython de cette machine) : reproduire le cas en vraie grandeur
demanderait des centaines de milliers de clés, lent et dépendant de la machine.
Les tests abaissent donc le plafond de la connexion à 999 (`Connection.setlimit`,
la valeur historique de SQLITE_MAX_VARIABLE_NUMBER) et vérifient que le
découpage par lots de 900 de `_in_chunks` passe dessous — déterministe quelle
que soit la build.

Ces tests couvrent les deux cas que l'ancien code confondait :
- au-delà de 999 clés, TOUS les labels demandés doivent revenir (échoue sur
  l'ancien code : il renvoyait {}) ;
- une base sans table `label_styles` (construite avant D5) doit toujours
  renvoyer {} sans lever -- repli légitime, à ne pas casser en durcissant.

Lancer : python3 -m unittest tests.test_label_style_counts_chunk -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CRATE_DATA_DIR", tempfile.mkdtemp(prefix="radar-test-"))

from radar_web.radar import discogs_dump as dd  # noqa: E402

N_LABELS = 2500          # > 2 lots de 900, et > le plafond abaissé ci-dessous
VAR_LIMIT = 999          # plafond imposé à la connexion de test


@unittest.skipUnless(hasattr(sqlite3.Connection, "setlimit"),
                     "sqlite3.Connection.setlimit requiert Python 3.11+")
class LabelStyleCountsChunkTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "dump.sqlite3")
        self._orig_db_path, self._orig_connect = dd.DB_PATH, dd.connect_readonly
        dd.DB_PATH = self.db
        dd.connect_readonly = self._connect_limited
        self.addCleanup(self._restore)

    def _connect_limited(self):
        """Connexion de lecture au plafond de paramètres abaissé — c'est elle
        que `label_style_counts` ouvre, donc c'est elle qui doit être bridée."""
        if not dd.available():
            return None
        con = sqlite3.connect(self.db)
        con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, VAR_LIMIT)
        return con

    def _restore(self):
        dd.DB_PATH, dd.connect_readonly = self._orig_db_path, self._orig_connect
        self.tmp.cleanup()

    def _build(self, with_label_styles=True):
        con = sqlite3.connect(self.db)
        if with_label_styles:
            con.execute("CREATE TABLE label_styles (label_key TEXT, style TEXT, n INTEGER)")
            con.executemany(
                "INSERT INTO label_styles (label_key, style, n) VALUES (?,?,?)",
                [(f"label{i:05d}", "house", i + 1) for i in range(N_LABELS)])
        else:
            con.execute("CREATE TABLE releases (id INTEGER)")   # base non vide, mais sans label_styles
        con.commit()
        con.close()

    def test_plafond_de_parametres_depasse_reproduit(self):
        """Garde-fou du garde-fou : sans découpage, la requête lève bien —
        sinon les tests ci-dessous passeraient aussi sur le code bogué."""
        self._build()
        con = self._connect_limited()
        keys = [f"label{i:05d}" for i in range(N_LABELS)]
        qmarks = ",".join("?" * len(keys))
        with self.assertRaises(sqlite3.OperationalError):
            con.execute(
                f"SELECT label_key FROM label_styles WHERE label_key IN ({qmarks})", keys)
        con.close()

    def test_au_dela_du_plafond_tous_les_labels_reviennent(self):
        """Le cas B1 : l'ancien code renvoyait {} ici, en silence."""
        self._build()
        keys = [f"label{i:05d}" for i in range(N_LABELS)]
        out = dd.label_style_counts(keys)
        self.assertEqual(len(out), N_LABELS)
        self.assertEqual(out["label00000"], {"house": 1})
        self.assertEqual(out[f"label{N_LABELS - 1:05d}"], {"house": N_LABELS})

    def test_sous_le_plafond_inchange(self):
        """Non-régression du chemin déjà correct avant le correctif."""
        self._build()
        out = dd.label_style_counts([f"label{i:05d}" for i in range(10)])
        self.assertEqual(len(out), 10)
        self.assertEqual(out["label00005"], {"house": 6})

    def test_cle_inconnue_absente_du_resultat(self):
        self._build()
        out = dd.label_style_counts(["label00001", "jamais-vu"])
        self.assertEqual(set(out), {"label00001"})

    def test_table_label_styles_absente_renvoie_dict_vide(self):
        """Repli légitime (base construite avant D5) : {} sans lever.
        Ce cas DOIT rester distinct du précédent -- c'est la confusion entre
        les deux qui rendait le bug B1 invisible."""
        self._build(with_label_styles=False)
        self.assertEqual(dd.label_style_counts(["label00000"]), {})
        self.assertEqual(dd.label_style_counts([f"label{i:05d}" for i in range(N_LABELS)]), {})

    def test_dump_absent_renvoie_dict_vide(self):
        dd.DB_PATH = os.path.join(self.tmp.name, "inexistant.sqlite3")
        self.assertEqual(dd.label_style_counts(["label00000"]), {})


if __name__ == "__main__":
    unittest.main()
