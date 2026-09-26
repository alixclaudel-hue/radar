"""Tests unitaires pour scripts/hooks/session_context.py — zéro appel réseau.

Le hook de contexte n'a pas le droit d'échouer et n'a pas le droit d'appeler le
réseau au moment où une session s'ouvre. Les deux propriétés sont donc vérifiées
ici mécaniquement :

- **jamais bloquant** : `main()` rend toujours 0, y compris sur un manifeste
  absent, illisible ou à moitié écrit ;
- **jamais d'appel dans le chemin de session** : le pavage part en arrière-plan
  détaché, et `subprocess.Popen` est remplacé dans les tests — aucun processus
  n'est réellement créé, donc aucune requête ne peut partir.

Le repli sur `~/radar/.env` de `_presence_cle()` est neutralisé par un
`expanduser` bouchonné : la suite ne doit pas dépendre de ce que contient le
fichier de la machine qui l'exécute.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _RACINE not in sys.path:
    sys.path.insert(0, _RACINE)

# Chargement dynamique : le hook vit hors paquet, comme telemetry.py.
_CHEMIN = os.path.join(_RACINE, "scripts", "hooks", "session_context.py")
_spec = importlib.util.spec_from_file_location("session_context", _CHEMIN)
session_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_context)

from scripts.ai import context_cache, prompts  # noqa: E402


def _ecrire(chemin: str, contenu: str) -> str:
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as handle:
        handle.write(contenu)
    return chemin


def _manifeste(chemin: str, documents) -> str:
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "documents": documents}, handle)
    return chemin


class ManifesteTests(unittest.TestCase):
    """Lecture tolérante du manifeste : filtrer, jamais lever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = self.tmp.name

    def test_manifeste_absent_rend_liste_vide(self):
        self.assertEqual(session_context.load_manifest(os.path.join(self.racine, "rien.json")), [])

    def test_manifeste_json_corrompu_rend_liste_vide(self):
        chemin = _ecrire(os.path.join(self.racine, "casse.json"), "{ ceci n'est pas du json")
        self.assertEqual(session_context.load_manifest(chemin), [])

    def test_manifeste_racine_non_objet_rend_liste_vide(self):
        chemin = _ecrire(os.path.join(self.racine, "liste.json"), "[1, 2, 3]")
        self.assertEqual(session_context.load_manifest(chemin), [])

    def test_documents_non_liste_rend_liste_vide(self):
        chemin = _ecrire(os.path.join(self.racine, "d.json"), '{"documents": {"a": 1}}')
        self.assertEqual(session_context.load_manifest(chemin), [])

    def test_entrees_sans_path_ecartees_les_autres_gardees(self):
        chemin = _manifeste(
            os.path.join(self.racine, "m.json"),
            [
                {"path": "CLAUDE.md", "why": "ok"},
                {"why": "pas de path"},
                {"path": "   ", "why": "blanc"},
                "pas un objet",
                {"path": " docs/a.md ", "why": "espaces autour"},
            ],
        )
        documents = session_context.load_manifest(chemin)
        self.assertEqual([d["path"] for d in documents], ["CLAUDE.md", "docs/a.md"])

    def test_why_tronque_a_la_borne(self):
        chemin = _manifeste(
            os.path.join(self.racine, "m.json"),
            [{"path": "a.md", "why": "x" * 500}],
        )
        document = session_context.load_manifest(chemin)[0]
        self.assertEqual(len(document["why"]), session_context.MAX_WHY)

    def test_why_absent_devient_chaine_vide(self):
        chemin = _manifeste(os.path.join(self.racine, "m.json"), [{"path": "a.md"}])
        self.assertEqual(session_context.load_manifest(chemin)[0]["why"], "")

    def test_manifest_path_defaut_dans_docs(self):
        with patch.dict(os.environ, {}, clear=True):
            chemin = session_context.manifest_path("/depot")
        self.assertEqual(chemin, os.path.join("/depot", "docs", "context-manifest.json"))

    def test_manifest_path_surcharge_par_variable(self):
        with patch.dict(os.environ, {"RADAR_CONTEXT_MANIFEST": "/tmp/ailleurs.json"}, clear=True):
            self.assertEqual(session_context.manifest_path("/depot"), "/tmp/ailleurs.json")

    def test_resoudre_relatif_et_absolu(self):
        self.assertEqual(
            session_context._resoudre("/depot", "docs/a.md"), os.path.normpath("/depot/docs/a.md")
        )
        self.assertEqual(session_context._resoudre("/depot", "/x/a.md"), "/x/a.md")


class PresenceCleTests(unittest.TestCase):
    """On teste la présence d'une clé, jamais sa valeur."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projet = self.tmp.name
        self.ailleurs = os.path.join(self.tmp.name, "ailleurs-sans-env")
        os.makedirs(self.ailleurs, exist_ok=True)

    def _presence(self, environ):
        with (
            patch.dict(os.environ, environ, clear=True),
            patch.object(os.path, "expanduser", return_value=self.ailleurs),
        ):
            return session_context._presence_cle(self.projet)

    def test_aucune_cle_nulle_part(self):
        self.assertFalse(self._presence({}))

    def test_cle_en_environnement(self):
        self.assertTrue(self._presence({"OPENROUTER_API_KEY": "sk-valeur-connue"}))

    def test_cle_suffixee_en_environnement(self):
        self.assertTrue(self._presence({"OPENROUTER_API_KEY_2": "sk-rotation"}))

    def test_cle_en_environnement_mais_vide_ignoree(self):
        self.assertFalse(self._presence({"DEEPSEEK_API_KEY": "   "}))

    def test_nom_proche_mais_pas_une_cle(self):
        self.assertFalse(self._presence({"OPENROUTER_KEY": "sk-pas-le-bon-nom"}))

    def test_cle_dans_le_env_du_depot(self):
        _ecrire(
            os.path.join(self.projet, ".env"),
            "# commentaire\nGEMINI_API_KEY=AIza-quelque-chose\n",
        )
        self.assertTrue(self._presence({}))

    def test_env_du_depot_sans_cle(self):
        _ecrire(os.path.join(self.projet, ".env"), "AUTRE_CHOSE=1\nSANS_EGAL\n")
        self.assertFalse(self._presence({}))

    def test_est_nom_de_cle_reconnait_les_familles(self):
        for nom in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY"):
            self.assertTrue(session_context._est_nom_de_cle(nom))
            self.assertTrue(session_context._est_nom_de_cle(nom + "_3"))
        self.assertFalse(session_context._est_nom_de_cle("PATH"))


class EtatDocumentsTests(unittest.TestCase):
    """L'état du cache est calculé sans jamais lever, fichiers absents compris."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = self.tmp.name

    def test_fichier_absent_ecarte_avant_prepare(self):
        present = _ecrire(os.path.join(self.racine, "present.md"), "contenu\n")
        absent = os.path.join(self.racine, "absent.md")
        paves, a_analyser, manquants = session_context.etat_documents([present, absent])
        self.assertEqual(paves, [])
        self.assertEqual([d["path"] for d in a_analyser], [present])
        self.assertEqual(manquants, {absent})

    def test_liste_vide_rend_tout_vide(self):
        paves, a_analyser, manquants = session_context.etat_documents([])
        self.assertEqual((paves, a_analyser, manquants), ([], [], set()))

    def test_document_deja_analyse_entre_dans_paves(self):
        chemin = _ecrire(os.path.join(self.racine, "doc.md"), "ligne un\nligne deux\n")
        with open(chemin, "r", encoding="utf-8") as handle:
            envoi = prompts.numbered_lines(handle.read())
        analyse = {"path": chemin, "key_points": ["point connu"]}
        index = {"version": 1, "documents": {context_cache.cle_document(chemin, envoi): {"analyse": analyse}}}
        with patch.object(context_cache, "load_index", return_value=index):
            paves, a_analyser, manquants = session_context.etat_documents([chemin])
        self.assertEqual([d["path"] for d in paves], [chemin])
        self.assertEqual(a_analyser, [])
        self.assertEqual(manquants, set())

    def test_sans_module_de_cache_tout_est_a_analyser(self):
        chemin = _ecrire(os.path.join(self.racine, "doc.md"), "contenu\n")
        with patch.object(session_context, "context_cache", None):
            paves, a_analyser, manquants = session_context.etat_documents([chemin])
        self.assertEqual(paves, [])
        self.assertEqual([d["path"] for d in a_analyser], [chemin])
        self.assertEqual(manquants, set())


class ResumeCompactTests(unittest.TestCase):
    """Le condensé doit être borné en nombre de points et en longueur."""

    def test_points_cles_bornes_en_nombre(self):
        analyse = {"key_points": ["un", "deux", "trois", "quatre", "cinq"]}
        resume = session_context._resume_compact(analyse)
        self.assertEqual(resume, "un · deux · trois")

    def test_point_tronque_a_la_borne(self):
        analyse = {"key_points": ["y" * 900]}
        self.assertEqual(len(session_context._resume_compact(analyse)), session_context.MAX_POINT_CHARS)

    def test_repli_sur_les_sections(self):
        analyse = {"sections": [{"summary": "premier"}, {"summary": "second"}]}
        self.assertEqual(session_context._resume_compact(analyse), "premier · second")

    def test_entree_non_dict_rend_chaine_vide(self):
        self.assertEqual(session_context._resume_compact(None), "")
        self.assertEqual(session_context._resume_compact("texte"), "")

    def test_points_vides_ignores(self):
        analyse = {"key_points": ["", "   ", "utile"]}
        self.assertEqual(session_context._resume_compact(analyse), "utile")


class DigestTests(unittest.TestCase):
    """Le texte injecté en session : borné, lisible, sans surprise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = self.tmp.name

    def _digest(self, documents, resolus, etat, **kwargs):
        params = {"rafraichi": False, "rien_a_paver": False, "motif_sans_rafraichissement": ""}
        params.update(kwargs)
        return session_context.construire_digest(documents, resolus, etat, **params)

    def test_digest_nominal_liste_les_documents(self):
        documents = [
            {"path": "CLAUDE.md", "why": "règles"},
            {"path": "docs/absent.md", "why": "parti"},
        ]
        resolus = [os.path.join(self.racine, "CLAUDE.md"), os.path.join(self.racine, "docs/absent.md")]
        _ecrire(resolus[0], "x" * 2048)
        etat = ([], [{"path": resolus[0]}], {resolus[1]})
        texte = self._digest(documents, resolus, etat, rien_a_paver=False)
        self.assertIn("Documents déclarés : 2 · analysés en cache : 0 · à analyser : 1", texte)
        self.assertIn("- CLAUDE.md — à analyser — 2.0 ko — règles", texte)
        self.assertIn("- docs/absent.md — manquant — ? — parti", texte)
        self.assertIn("--mode context", texte)

    def test_digest_affiche_le_resume_des_documents_analyses(self):
        chemin = os.path.join(self.racine, "a.md")
        _ecrire(chemin, "contenu\n")
        documents = [{"path": "a.md", "why": ""}]
        etat = ([{"path": chemin, "analyse": {"key_points": ["le point clé"]}}], [], set())
        texte = self._digest(documents, [chemin], etat)
        self.assertIn("- a.md — analysé", texte)
        self.assertIn("le point clé", texte)

    def test_digest_annonce_le_pavage_lance(self):
        texte = self._digest([{"path": "a.md", "why": ""}], ["/x/a.md"], ([], [{"path": "/x/a.md"}], set()), rafraichi=True)
        self.assertIn("Pavage des documents manquants lancé en arrière-plan détaché.", texte)

    def test_digest_annonce_le_cache_a_jour(self):
        texte = self._digest([{"path": "a.md", "why": ""}], ["/x/a.md"], ([], [], set()), rien_a_paver=True)
        self.assertIn("Cache à jour : rien à paver.", texte)

    def test_digest_motif_sans_rafraichissement(self):
        texte = self._digest(
            [{"path": "a.md", "why": ""}],
            ["/x/a.md"],
            ([], [{"path": "/x/a.md"}], set()),
            motif_sans_rafraichissement=" (RADAR_CONTEXT_AUTO désactivé)",
        )
        self.assertIn("Aucun rafraîchissement lancé (RADAR_CONTEXT_AUTO désactivé).", texte)

    def test_digest_tronque_au_dela_de_la_borne(self):
        documents = [{"path": f"docs/f{i}.md", "why": "z" * 200} for i in range(40)]
        resolus = [f"/depot/docs/f{i}.md" for i in range(40)]
        etat = ([], [{"path": r} for r in resolus], set())
        texte = self._digest(documents, resolus, etat)
        self.assertTrue(texte.endswith("[digest tronqué]"))
        self.assertLessEqual(len(texte), session_context.MAX_DIGEST + len("\n[digest tronqué]"))


class RafraichissementTests(unittest.TestCase):
    """Le lancement part détaché, ou ne part pas du tout — jamais bloquant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = os.path.join(self.tmp.name, "sous", "session-refresh.log")

    def test_rien_a_paver_ne_lance_rien(self):
        with patch.object(session_context.subprocess, "Popen") as popen:
            self.assertFalse(session_context.lancer_rafraichissement([], self.journal))
        popen.assert_not_called()

    def test_sans_module_de_cache_ne_lance_rien(self):
        with (
            patch.object(session_context, "context_cache", None),
            patch.object(session_context.subprocess, "Popen") as popen,
        ):
            self.assertFalse(session_context.lancer_rafraichissement(["/x/a.md"], self.journal))
        popen.assert_not_called()

    def test_lancement_detache_avec_le_mode_context(self):
        with patch.object(session_context.subprocess, "Popen") as popen:
            lance = session_context.lancer_rafraichissement(["/x/a.md", "/x/b.md"], self.journal)
        self.assertTrue(lance)
        args, kwargs = popen.call_args
        commande = args[0]
        self.assertEqual(commande[0], sys.executable)
        self.assertIn("--mode", commande)
        self.assertEqual(commande[commande.index("--mode") + 1], "context")
        self.assertIn("--no-sleep", commande)
        self.assertEqual(commande.count("-f"), 2)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], session_context.subprocess.DEVNULL)

    def test_echec_de_lancement_rend_faux(self):
        with patch.object(session_context.subprocess, "Popen", side_effect=OSError("boom")):
            self.assertFalse(session_context.lancer_rafraichissement(["/x/a.md"], self.journal))


class MainTests(unittest.TestCase):
    """`main()` : toujours 0, jamais d'appel réel, jamais de sortie sur manifeste vide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projet = self.tmp.name
        self.manifeste = os.path.join(self.projet, "docs", "context-manifest.json")
        self.document = os.path.join(self.projet, "CLAUDE.md")
        _ecrire(self.document, "règles du projet\n")

    def _main(self, environ):
        base = {
            "CLAUDE_PROJECT_DIR": self.projet,
            "RADAR_CONTEXT_MANIFEST": self.manifeste,
        }
        base.update(environ)
        sortie = io.StringIO()
        with (
            patch.dict(os.environ, base, clear=True),
            patch.object(os.path, "expanduser", return_value=os.path.join(self.tmp.name, "vide")),
            contextlib.redirect_stdout(sortie),
        ):
            code = session_context.main()
        return code, sortie.getvalue()

    def test_manifeste_absent_rend_zero_sans_sortie(self):
        code, sortie = self._main({})
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_digest_imprime_et_code_zero(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": "règles"}])
        code, sortie = self._main({"RADAR_CONTEXT_AUTO": "0"})
        self.assertEqual(code, 0)
        self.assertIn("Contexte de session — digest automatique", sortie)
        self.assertIn("- CLAUDE.md — à analyser", sortie)
        self.assertIn("RADAR_CONTEXT_AUTO désactivé", sortie)

    def test_aucun_lancement_quand_desactive(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": ""}])
        with patch.object(session_context.subprocess, "Popen") as popen:
            self._main({"RADAR_CONTEXT_AUTO": "0"})
        popen.assert_not_called()

    def test_aucun_lancement_en_auto_sans_cle(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": ""}])
        with (
            patch.object(session_context, "_presence_cle", return_value=False),
            patch.object(session_context.subprocess, "Popen") as popen,
        ):
            _, sortie = self._main({"RADAR_CONTEXT_AUTO": "auto"})
        popen.assert_not_called()
        self.assertIn("aucune clé fournisseur détectée", sortie)

    def test_lancement_force_meme_sans_cle(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": ""}])
        with (
            patch.object(session_context, "_presence_cle", return_value=False),
            patch.object(session_context.subprocess, "Popen") as popen,
        ):
            _, sortie = self._main({"RADAR_CONTEXT_AUTO": "1"})
        popen.assert_called_once()
        self.assertIn("Pavage des documents manquants lancé en arrière-plan détaché.", sortie)

    def test_lancement_en_auto_quand_une_cle_existe(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": ""}])
        with (
            patch.object(session_context, "_presence_cle", return_value=True),
            patch.object(session_context.subprocess, "Popen") as popen,
        ):
            self._main({"RADAR_CONTEXT_AUTO": "auto"})
        popen.assert_called_once()

    def test_rien_a_paver_ne_lance_rien_meme_force(self):
        _manifeste(self.manifeste, [{"path": "CLAUDE.md", "why": ""}])
        with open(self.document, "r", encoding="utf-8") as handle:
            envoi = prompts.numbered_lines(handle.read())
        index = {
            "version": 1,
            "documents": {
                context_cache.cle_document(self.document, envoi): {"analyse": {"key_points": ["déjà là"]}}
            },
        }
        with (
            patch.object(context_cache, "load_index", return_value=index),
            patch.object(session_context.subprocess, "Popen") as popen,
        ):
            _, sortie = self._main({"RADAR_CONTEXT_AUTO": "1"})
        popen.assert_not_called()
        self.assertIn("Cache à jour : rien à paver.", sortie)

    def test_ne_leve_jamais_meme_sur_manifeste_corrompu(self):
        _ecrire(self.manifeste, "{ pas du json")
        code, sortie = self._main({"RADAR_CONTEXT_AUTO": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")


if __name__ == "__main__":
    unittest.main()
