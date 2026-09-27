"""`_disco_resolve` doit retomber sur le `hint` (nom transmis par le graphe
label/artiste) plutôt que sur la clé normalisée quand l'entité n'a jamais été
résolue côté utilisateur (`resolved.json`/`artists_resolved.json` ne couvrent
que son propre corpus, pas les voisins découverts par un graphe) -- sinon la
discographie affichée juste après la construction d'un graphe utilisait la
clé brute (minuscules) comme nom ET comme requête Discogs.

Lancer : python3 -m unittest tests.test_disco_resolve_hint -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from radar_web import app as appmod  # noqa: E402


class _FakeCtx:
    def __init__(self, resolved=None, artists_res=None):
        self.resolved = resolved or {}
        self.artists_res = artists_res or {}


class DiscoResolveHintTestCase(unittest.TestCase):
    def test_label_inconnu_utilise_le_hint_du_graphe(self):
        c = _FakeCtx()
        name, qval = appmod._disco_resolve(c, "label", "musical freedom", hint="Musical Freedom")
        self.assertEqual(name, "Musical Freedom")
        self.assertEqual(qval, "Musical Freedom")

    def test_label_deja_resolu_prime_sur_le_hint(self):
        c = _FakeCtx(resolved={"musical freedom": {"discogs_name": "Musical Freedom Records"}})
        name, qval = appmod._disco_resolve(c, "label", "musical freedom", hint="Musical Freedom")
        self.assertEqual(name, "Musical Freedom Records")
        self.assertEqual(qval, "Musical Freedom Records")

    def test_label_sans_hint_ni_resolution_retombe_sur_la_cle(self):
        c = _FakeCtx()
        name, qval = appmod._disco_resolve(c, "label", "musical freedom")
        self.assertEqual(name, "musical freedom")
        self.assertEqual(qval, "musical freedom")

    def test_artiste_inconnu_utilise_le_hint_du_graphe(self):
        c = _FakeCtx()
        name, qval = appmod._disco_resolve(c, "artist", "some artist", hint="Some Artist")
        self.assertEqual(name, "Some Artist")
        self.assertEqual(qval, "Some Artist")


if __name__ == "__main__":
    unittest.main()
