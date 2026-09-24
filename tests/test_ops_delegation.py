import os
import tempfile
import unittest
from unittest.mock import patch

from radar_ops import delegation


class TestOpsDelegation(unittest.TestCase):
    """Suite de tests pour le module radar_ops.delegation."""

    def test_log_dir_par_defaut(self):
        """Vérifie que le dossier des journaux par défaut utilise data_root/ops."""
        with patch.dict(os.environ, {}, clear=True):
            # Supprime la variable si elle existe
            os.environ.pop("RADAR_OPS_TELEMETRY_DIR", None)
            resultat = delegation.log_dir("/data/racine")
            self.assertEqual(resultat, os.path.join("/data/racine", "ops"))

    def test_log_dir_avec_variable_environnement(self):
        """Vérifie que RADAR_OPS_TELEMETRY_DIR est prioritaire."""
        env = {"RADAR_OPS_TELEMETRY_DIR": "/custom/path/telemetry"}
        with patch.dict(os.environ, env):
            resultat = delegation.log_dir("/data/racine")
            self.assertEqual(resultat, "/custom/path/telemetry")

    def test_read_jsonl_fichier_absent(self):
        """Vérifie que read_jsonl renvoie [] sur un fichier absent."""
        resultat = delegation.read_jsonl("/chemin/inexistant/vers/fichier.jsonl")
        self.assertEqual(resultat, [])

    def test_read_jsonl_cas_limites_et_erreurs(self):
        """Vérifie le comportement avec lignes vides, JSON invalide, non-objets et sans ts."""
        contenu = (
            "\n"  # Ligne vide
            "   \n"  # Espaces uniquement
            "{invalid json\n"  # JSON invalide
            "[1, 2, 3]\n"  # Non-objet (liste)
            '{"not_ts": 123}\n'  # Sans champ ts numérique
            '{"ts": "pas_un_nombre"}\n'  # ts non numérique
            '{"ts": 1000, "valeur": "ok"}\n'  # Cas nominal valide
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            chemin = os.path.join(tmpdir, "test.jsonl")
            with open(chemin, "w", encoding="utf-8") as f:
                f.write(contenu)

            resultat = delegation.read_jsonl(chemin)
            self.assertEqual(len(resultat), 1)
            self.assertEqual(resultat[0]["ts"], 1000.0)
            self.assertEqual(resultat[0]["valeur"], "ok")

    def test_read_jsonl_filtre_since_ts_et_limit(self):
        """Vérifie le filtrage par since_ts et l'application de la limite."""
        lignes = [
            '{"ts": 100}\n',
            '{"ts": 200}\n',
            '{"ts": 300}\n',
            '{"ts": 400}\n',
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            chemin = os.path.join(tmpdir, "test.jsonl")
            with open(chemin, "w", encoding="utf-8") as f:
                f.writelines(lignes)

            # Filtrage since_ts
            res_since = delegation.read_jsonl(chemin, since_ts=250)
            self.assertEqual([r["ts"] for r in res_since], [300.0, 400.0])

            # Application de la limite (dernières lignes)
            res_limit = delegation.read_jsonl(chemin, limit=2)
            self.assertEqual([r["ts"] for r in res_limit], [300.0, 400.0])

            # Combinaison since_ts et limit
            res_both = delegation.read_jsonl(chemin, since_ts=150, limit=2)
            self.assertEqual([r["ts"] for r in res_both], [300.0, 400.0])

    def test_summarize_median_ms_aucun_elapsed(self):
        """Vérifie que totals['median_ms'] vaut None et non 0 sans elapsed_ms."""
        receipts = [
            {"ts": 100, "status": "ok"},
            {"ts": 200, "status": "ok", "elapsed_ms": None},
        ]
        events = []
        sommaire = delegation.summarize(receipts, events)
        self.assertIsNone(sommaire["totals"]["median_ms"])

    def test_summarize_err_compte_statuts_differents_de_ok(self):
        """Vérifie que 'err' compte tout status différent de 'ok'."""
        receipts = [
            {"ts": 100, "status": "ok"},
            {"ts": 200, "status": "error"},
            {"ts": 300},  # Statut absent -> considéré comme erreur
            {"ts": 400, "status": "fail"},
        ]
        sommaire = delegation.summarize(receipts, receipts)  # événements quelconques
        self.assertEqual(sommaire["totals"]["ok"], 1)
        self.assertEqual(sommaire["totals"]["err"], 3)

    def test_somme_jetons_champ_absent_vaut_zero(self):
        """Vérifie que les jetons s'additionnent en traitant un champ absent comme 0."""
        receipts = [
            {"ts": 100, "prompt_tokens": 50},  # output_tokens absent
            {"ts": 200, "output_tokens": 30},  # prompt_tokens absent
            {"ts": 300},  # tous deux absents
        ]
        sommaire = delegation.summarize(receipts, [])
        self.assertEqual(sommaire["totals"]["prompt_tokens"], 50)
        self.assertEqual(sommaire["totals"]["output_tokens"], 30)
        self.assertEqual(sommaire["totals"]["total_tokens"], 0)

    def test_group_by_mode_et_by_model(self):
        """Vérifie le tri par total_tokens décroissant et l'attribut 'inconnu'."""
        receipts = [
            {"ts": 100, "mode": "rapide", "model": "gemini-1", "total_tokens": 10},
            {"ts": 200, "total_tokens": 100},  # mode et model absents -> 'inconnu'
            {"ts": 300, "mode": "rapide", "model": "gemini-2", "total_tokens": 50},
        ]
        sommaire = delegation.summarize(receipts, [])

        # by_mode : 'inconnu' (100) doit être avant 'rapide' (60) car trié par total_tokens décroissant
        modes = sommaire["by_mode"]
        self.assertEqual(modes[0]["mode"], "inconnu")
        self.assertEqual(modes[0]["total_tokens"], 100)
        self.assertEqual(modes[1]["mode"], "rapide")
        self.assertEqual(modes[1]["total_tokens"], 60)

        # by_model : 'inconnu' (100), puis 'gemini-2' (50), puis 'gemini-1' (10)
        models = sommaire["by_model"]
        self.assertEqual(models[0]["model"], "inconnu")
        self.assertEqual(models[1]["model"], "gemini-2")
        self.assertEqual(models[2]["model"], "gemini-1")

    def test_by_day_structure_et_trous(self):
        """Vérifie que by_day renvoie exactement days entrées, trous compris."""
        # Fixer un now_ts correspondant à un jour précis (ex: 2023-01-10 12:00:00 UTC)
        # 1673352000 = 2023-01-10 12:00:00 UTC
        now_ts = 1673352000.0
        days_count = 3

        receipts = [
            # 2023-01-10 (aujourd'hui)
            {"ts": 1673352000, "total_tokens": 100},
            # 2023-01-08 (il y a 2 jours, un jour sera sauté: 2023-01-09)
            {"ts": 1673352000 - 2 * 86400, "total_tokens": 200},
        ]

        resultat = delegation._by_day(receipts, now_ts, days_count)

        self.assertEqual(len(resultat), 3)
        # Plus ancien en premier (2023-01-08)
        self.assertEqual(resultat[0]["date"], "2023-01-08")
        self.assertEqual(resultat[0]["calls"], 1)
        self.assertEqual(resultat[0]["total_tokens"], 200)

        # Jour vide (2023-01-09), calls à 0
        self.assertEqual(resultat[1]["date"], "2023-01-09")
        self.assertEqual(resultat[1]["calls"], 0)
        self.assertEqual(resultat[1]["total_tokens"], 0)

        # Aujourd'hui (2023-01-10)
        self.assertEqual(resultat[2]["date"], "2023-01-10")
        self.assertEqual(resultat[2]["calls"], 1)
        self.assertEqual(resultat[2]["total_tokens"], 100)

    def test_sessions_tri_et_comptages(self):
        """Vérifie le tri des sessions, le comptage des prompts, tool_calls, tool_errs et top_tools."""
        events = [
            {"ts": 100, "session": "s1", "kind": "prompt"},
            {"ts": 200, "session": "s1", "kind": "tool", "tool": "git", "ok": True},
            {"ts": 300, "session": "s1", "kind": "tool", "tool": "grep", "ok": False},
            {"ts": 500, "session": "s2", "kind": "prompt"},  # Plus récente
        ]
        sessions = delegation._sessions(events)

        self.assertEqual(len(sessions), 2)
        # Trié par last_ts décroissant : s2 d'abord, puis s1
        self.assertEqual(sessions[0]["session"], "s2")
        self.assertEqual(sessions[1]["session"], "s1")

        s1_data = sessions[1]
        self.assertEqual(s1_data["prompts"], 1)
        self.assertEqual(s1_data["tool_calls"], 2)
        self.assertEqual(s1_data["tool_errs"], 1)
        self.assertEqual(len(s1_data["top_tools"]), 2)

    def test_sessions_top_tools_plafonne_a_cinq(self):
        """Vérifie que top_tools garde les 5 outils les plus fréquents, et eux seuls."""
        events = []
        # Sept outils distincts, de fréquence décroissante : les deux plus
        # rares doivent disparaître, jamais l'un des cinq premiers.
        for rang, nom in enumerate(["a", "b", "c", "d", "e", "f", "g"]):
            for i in range(7 - rang):
                events.append({"ts": 100 + rang * 10 + i, "session": "s1",
                               "kind": "tool", "tool": nom, "ok": True})
        top = delegation._sessions(events)[0]["top_tools"]
        self.assertEqual([t["name"] for t in top], ["a", "b", "c", "d", "e"])
        self.assertEqual(top[0]["count"], 7)

    def test_delegation_per_tool_call_ratio(self):
        """Vérifie per_tool_call (None sans outil, ratio arrondi sinon)."""
        # Cas sans événement tool
        sommaire_sans_tool = delegation.summarize([{"ts": 100}], [{"ts": 100, "kind": "prompt"}])
        self.assertIsNone(sommaire_sans_tool["delegation"]["per_tool_call"])

        # Cas avec événements tool
        receipts = [{"ts": 100}, {"ts": 101}, {"ts": 102}]  # 3 reçus
        events = [
            {"ts": 100, "kind": "tool"},
            {"ts": 101, "kind": "tool"},
        ]  # 2 tool_calls -> 3 / 2 = 1.5
        sommaire_avec_tool = delegation.summarize(receipts, events)
        self.assertEqual(sommaire_avec_tool["delegation"]["per_tool_call"], 1.5)

    def test_snapshot_dossier_vide(self):
        """Vérifie que snapshot sur dossier vide renvoie missing avec les deux fichiers et totaux à zéro sans lever."""
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.dict(os.environ, {}, clear=True):
            # Sans ce nettoyage, une machine où RADAR_OPS_TELEMETRY_DIR est
            # définie ferait lire un autre dossier que celui du test.
            res = delegation.snapshot(tmpdir, days=14)

            self.assertEqual(res["receipts_seen"], 0)
            self.assertEqual(res["events_seen"], 0)
            self.assertEqual(res["totals"]["calls"], 0)
            self.assertEqual(res["totals"]["total_tokens"], 0)
            self.assertIn(delegation.RECEIPTS_NAME, res["missing"])
            self.assertIn(delegation.EVENTS_NAME, res["missing"])
            self.assertEqual(len(res["missing"]), 2)


class ReceiptsPathTests(unittest.TestCase):
    """`_receipts_path()` -- depuis la suppression du transport git (commit
    e35af25), ce chemin doit toujours pointer directement sous `<ops>/`,
    jamais vers un repli `CLAUDE_PROJECT_DIR` mort en conteneur ni vers une
    copie figée qu'un `os.path.exists()` préférerait indéfiniment."""

    def test_pointe_toujours_sous_data_root_ops(self):
        data_root = "/fake/data/root"
        with patch.dict(os.environ, {}, clear=True):
            attendu = os.path.join(data_root, "ops", delegation.RECEIPTS_NAME)
            self.assertEqual(delegation._receipts_path(data_root), attendu)

    def test_ignore_claude_project_dir_meme_avec_un_vrai_fichier(self):
        """Un `CLAUDE_PROJECT_DIR` défini, pointant vers un `.claude/` qui
        contient un vrai fichier de reçus, ne doit PAS être choisi -- c'est
        exactement le repli mort que ce correctif retire."""
        data_root = "/fake/data/root"
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = os.path.join(tmpdir, ".claude")
            os.makedirs(claude_dir, exist_ok=True)
            leurre = os.path.join(claude_dir, delegation.RECEIPTS_NAME)
            with open(leurre, "w", encoding="utf-8") as f:
                f.write('{"mode": "code", "status": "ok", "ts": 1}\n')

            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": tmpdir}, clear=True):
                resultat = delegation._receipts_path(data_root)
                self.assertNotEqual(resultat, leurre)
                self.assertEqual(
                    resultat, os.path.join(data_root, "ops", delegation.RECEIPTS_NAME))

    def test_respecte_radar_ops_telemetry_dir_comme_les_evenements(self):
        """Même override que `log_dir()` pour les événements : les deux
        journaux doivent rester sous le même dossier ops."""
        with patch.dict(os.environ, {"RADAR_OPS_TELEMETRY_DIR": "/custom/ops"}, clear=True):
            self.assertEqual(
                delegation._receipts_path("/fake/data/root"),
                os.path.join("/custom/ops", delegation.RECEIPTS_NAME))


if __name__ == "__main__":
    unittest.main()
