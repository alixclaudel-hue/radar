import json
import re
import unittest
import jinja2


class TestOpsJobsTemplate(unittest.TestCase):

    def setUp(self):
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader("radar_ops/templates"),
            autoescape=True
        )
        self.template = self.env.get_template("jobs.html")

    def test_rendu_sans_exception_listes_vides(self):
        """1. Rendu sans exception avec des listes queue/statuses vides."""
        data = {"queue": [], "statuses": []}
        html = self.template.render(page="jobs", data=data, sha="abcdef0")
        self.assertIn("Jobs &amp; file d'attente", html)
        self.assertIn("abcdef0", html)

    def test_rendu_job_running_elements_js(self):
        """2. Rendu avec un job running : présence des IDs attendus par le JS."""
        data = {
            "queue": [],
            "statuses": [
                {
                    "uid": "job_123",
                    "name": "import_discs",
                    "running": True,
                    "error": None,
                    "message": "",
                    "done": 5,
                    "total": 10,
                    "age_s": 0,
                    "pct": 50.0,
                    "rate_per_s": 1.0,
                    "rate_per_min": 60,
                    "eta_s": 5,
                }
            ],
        }
        html = self.template.render(page="jobs", data=data, sha="1234567")
        self.assertIn('id="worker-name"', html)
        self.assertIn('id="worker-gauge"', html)
        self.assertIn('id="worker-pct"', html)

    def test_rendu_queues_p0_p1_et_running(self):
        """3. Rendu avec files p0/p1 et job running : vérifie la présence des groupes SVG."""
        data = {
            "queue": [
                {"id": 1, "uid": "u1", "name": "job_p1", "ts": 1000, "state": "queued", "priority": 1, "wait_s": 10, "position": 0},
                {"id": 2, "uid": "u2", "name": "job_p0", "ts": 1001, "state": "queued", "priority": 0, "wait_s": 20, "position": 1},
                {"id": 3, "uid": "u3", "name": "job_run", "ts": 1002, "state": "running", "priority": 1, "wait_s": 0, "position": 0},
            ],
            "statuses": [],
        }
        html = self.template.render(page="jobs", data=data, sha="abcdef0")
        self.assertIn('id="tokens-p1"', html)
        self.assertIn('id="tokens-p0"', html)
        self.assertIn('id="badge-p1"', html)
        self.assertIn('id="badge-p0"', html)

    def test_rendu_erreur_echappement_securite(self):
        """4. Rendu avec message d'erreur malveillant : vérifie l'échappement JSON et l'absence d'injection."""
        malicious_msg = "<script>alert(1)</script> \"quoted\" & 'single'"
        data = {
            "queue": [],
            "statuses": [
                {
                    "uid": "err_1",
                    "name": "bad_job",
                    "running": False,
                    "error": malicious_msg,
                    "message": malicious_msg,
                    "done": 0,
                    "total": 10,
                    "age_s": 12,
                    "pct": 0.0,
                    "rate_per_s": 0.0,
                    "rate_per_min": 0,
                    "eta_s": None,
                }
            ],
        }
        html = self.template.render(page="jobs", data=data, sha="abcdef0")
        
        # Extraction du contenu du bloc <script id="boot"> via regex
        match = re.search(r'<script id="boot" type="application/json">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(match)
        boot_json_str = match.group(1)
        
        # Le contenu doit être parsable en JSON valide et retrouver exactement le message
        parsed_data = json.loads(boot_json_str)
        self.assertEqual(parsed_data["statuses"][0]["error"], malicious_msg)
        
        # Vérification qu'aucune balise <script> brute n'est injectée en clair dans le HTML en dehors du JSON
        self.assertNotIn("<script>alert(1)</script>", html.replace('<script id="boot" type="application/json">', '').replace('</script>', ''))

    def test_absence_anciens_tableaux_tbody(self):
        """5. Vérifie la régression : pas de <tbody id="statuses"> ni <tbody id="queue">."""
        data = {"queue": [], "statuses": []}
        html = self.template.render(page="jobs", data=data, sha="abcdef0")
        self.assertNotIn('id="statuses"', html)
        self.assertNotIn('id="queue"', html)

    def test_caracteres_unicode_et_html_speciaux(self):
        """6. Noms unicode et uid avec caractères spéciaux HTML : pas d'altération du HTML."""
        data = {
            "queue": [
                {
                    "id": 1,
                    "uid": "<uid&co>",
                    "name": "scorestore_évaluation",
                    "ts": 12345,
                    "state": "queued",
                    "priority": 1,
                    "wait_s": 5,
                    "position": 0,
                }
            ],
            "statuses": [],
        }
        html = self.template.render(page="jobs", data=data, sha="abcdef0")
        
        # Extraction et décodage du JSON pour s'assurer de l'intégrité des données
        match = re.search(r'<script id="boot" type="application/json">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(match)
        parsed_data = json.loads(match.group(1))
        
        self.assertEqual(parsed_data["queue"][0]["name"], "scorestore_évaluation")
        self.assertEqual(parsed_data["queue"][0]["uid"], "<uid&co>")
        
        # Vérification basique de structure HTML (balises de base héritées de base.html)
        self.assertIn("<!doctype html>", html)
