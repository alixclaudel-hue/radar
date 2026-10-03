"""Tests hors ligne de la sonde Bandcamp/Subsonic : seules les fonctions pures
qui reconnaissent une page de blocage sont testées (aucun réseau, aucun secret)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import probe_bandcamp_subsonic as sonde  # noqa: E402

# Page réellement servie par Bandcamp (Fastly) aux clients non navigateur, relevée le 01/10.
PAGE_FASTLY = ('<!DOCTYPE html><html><head><title>Client Challenge</title>'
               '<link href="/_fs-ch-1T1wmsGaOgGaSxcX/assets/styles.css" rel="stylesheet"></head>'
               '<body></body></html>')
PAGE_PROFIL = '<!DOCTYPE html><html><head><title>Un fan | Bandcamp</title></head><body data-blob="{}"></body></html>'


class TestClassify(unittest.TestCase):
    def test_page_fastly_est_un_defi(self):
        self.assertEqual(sonde.classify(PAGE_FASTLY, "text/html; charset=utf-8", 200), "challenge")

    def test_json_subsonic_valide(self):
        self.assertEqual(
            sonde.classify('{"subsonic-response":{"status":"ok"}}', "application/json", 200), "ok")

    def test_json_invalide(self):
        self.assertEqual(sonde.classify("{pas du json", "application/json", 200), "bad_json")

    def test_statut_erreur(self):
        self.assertEqual(sonde.classify("{}", "application/json", 500), "http_error")


class TestProfilDefi(unittest.TestCase):
    def test_page_fastly_detectee(self):
        # Régression : le contrôle 7 ne connaissait que les marqueurs Cloudflare
        # et affichait PASS devant le « Client Challenge » de Fastly.
        self.assertTrue(sonde.profile_html_challenge(PAGE_FASTLY, "text/html; charset=utf-8", 200))

    def test_vrai_profil_non_detecte(self):
        self.assertFalse(sonde.profile_html_challenge(PAGE_PROFIL, "text/html; charset=utf-8", 200))

    def test_statut_403(self):
        self.assertTrue(sonde.profile_html_challenge("", "text/html", 403))


if __name__ == "__main__":
    unittest.main()
