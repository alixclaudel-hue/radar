"""Vue « tous les retours » de /feedback (propriétaire, lecture seule).

Deux propriétés à tenir, dans cet ordre :

- le propriétaire voit les notes de TOUS les comptes, étiquetées de leur auteur ;
- tout autre compte n'en voit AUCUNE — l'isolation par-utilisateur reste la règle
  (cf. `paths.user_paths`). C'est `app._all_notes()` qui tranche en rendant
  `None`, et le gabarit teste seulement cette valeur : la règle n'est pas
  recopiée dans le HTML.

Tout est simulé (`load`, `paths.all_uids`, `paths.user_paths`, `accounts.get`)
pour ne jamais toucher le `CRATE_DATA_DIR` global : la suite partage un même
dossier de données entre modules, une écriture ici polluerait les autres tests.
"""
import html
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths  # noqa: E402

OWNER = paths.DEFAULT_UID           # "owner"
GUEST_A = "g_alice"
GUEST_B = "g_bob"

NOTES = {
    OWNER: [{"id": "nt_o", "ts": "2026-09-27 22:19", "page": "/settings",
             "target": "", "note": "note du propriétaire", "status": "nouveau",
             "gh_posted": True}],
    GUEST_A: [{"id": "nt_a", "ts": "2026-09-28 08:12", "page": "/reco-radar",
               "target": "", "note": "note d'Alice", "status": "nouveau",
               "gh_posted": True}],
    GUEST_B: [{"id": "nt_b", "ts": "2026-09-21 20:30", "page": "/patte",
               "target": "summary", "note": "note de Bob", "status": "nouveau",
               "gh_posted": False}],
}

ACCOUNTS = {OWNER: {"username": "owner"}, GUEST_A: {"username": "Alice"},
            GUEST_B: {"username": "Bob"}}


def _fake_get(uid):
    return ACCOUNTS.get(uid)


def _text(r):
    """Corps de réponse déséchappé : Jinja autoescape rend `d'Alice` en
    `d'Alice`, on compare donc sur le texte réellement affiché."""
    return html.unescape(r.text)


def _fake_user_paths(uid=OWNER):
    """Chemins factices : `user_paths` réel crée le dossier (makedirs), ce qu'on
    ne veut pas pour des uid bidon dans le dossier de données partagé."""
    return types.SimpleNamespace(
        ui_notes=f"/fake/users/{uid}/ui_notes.json",
        cart=f"/fake/users/{uid}/cart.json",
        config=f"/fake/users/{uid}/config.json",
        dir=f"/fake/users/{uid}", uid=uid)


def _fake_load(path, default=None):
    if os.path.basename(path) == "ui_notes.json":
        return list(NOTES.get(os.path.basename(os.path.dirname(path)), []))
    if os.path.basename(path) == "cart.json":
        return []
    return default if default is not None else []


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        for p in (
            mock.patch.object(appmod.paths, "all_uids",
                              return_value=[OWNER, GUEST_A, GUEST_B]),
            mock.patch.object(appmod.accounts, "get", side_effect=_fake_get),
            mock.patch.object(appmod, "load", side_effect=_fake_load),
            mock.patch.object(appmod.paths, "user_paths",
                              side_effect=_fake_user_paths),
            mock.patch.object(appmod.store, "read_config",
                              return_value={"token": ""}),
            mock.patch.object(appmod.websession, "dev_mode", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)


class AllNotesAggregationTest(_Base):
    def test_agrege_tous_les_comptes_tries_et_etiquetes(self):
        rows = appmod._all_notes(OWNER)
        # Tri décroissant sur `ts` : Alice (28/09) > owner (27/09) > Bob (21/09).
        self.assertEqual([r["id"] for r in rows], ["nt_a", "nt_o", "nt_b"])
        self.assertEqual([r["username"] for r in rows], ["Alice", "owner", "Bob"])
        # L'uid d'origine reste porté par la note (utile pour un futur « au nom de »).
        self.assertEqual([r["uid"] for r in rows], [GUEST_A, OWNER, GUEST_B])

    def test_pseudo_absent_replie_sur_l_uid(self):
        with mock.patch.object(appmod.accounts, "get", return_value=None):
            rows = appmod._all_notes(OWNER)
        self.assertEqual([r["username"] for r in rows], [GUEST_A, OWNER, GUEST_B])

    def test_isolation_autre_compte_rend_none(self):
        self.assertIsNone(appmod._all_notes(GUEST_A))
        self.assertIsNone(appmod._all_notes(GUEST_B))

    def test_uid_absent_rend_none(self):
        # Hors middleware, `current_uid()` vaut None : fail-closed, jamais de fuite.
        with mock.patch.object(appmod.store, "current_uid", return_value=None):
            self.assertIsNone(appmod._all_notes())


class OwnerFeedbackPageTest(_Base):
    def test_page_proprietaire_montre_les_notes_de_tous(self):
        r = self.client.get("/feedback")
        self.assertEqual(r.status_code, 200)
        t = _text(r)
        self.assertIn("Tous les retours (tous les utilisateurs)", t)
        self.assertIn("3 notes", t)
        for attendu in ("note d'Alice", "note de Bob", "note du propriétaire",
                        "Alice", "Bob"):
            self.assertIn(attendu, t)
        # Ordre du flux : Alice, puis owner, puis Bob. On se restreint à la
        # section agrégée : « Mes retours » en haut recontient la note du
        # propriétaire, qui fausserait la comparaison d'index.
        agg = t[t.index("Tous les retours (tous les utilisateurs)"):]
        self.assertLess(agg.index("note d'Alice"), agg.index("note du propriétaire"))
        self.assertLess(agg.index("note du propriétaire"), agg.index("note de Bob"))

    def test_vue_agregee_est_bien_lecture_seule(self):
        r = self.client.get("/feedback")
        # Un seul bouton de suppression : celui de la note propre du propriétaire.
        # Les notes d'Alice et de Bob n'en ont aucun (sinon on toucherait le
        # fichier du mauvais utilisateur, cf. commentaire du partiel).
        self.assertEqual(_text(r).count('hx-post="/feedback/delete"'), 1)


class GuestFeedbackPageTest(_Base):
    def setUp(self):
        super().setUp()
        # Simule une session d'invité : le middleware pose `current_uid = GUEST_A`.
        p = mock.patch.object(appmod, "_req_uid", side_effect=lambda request: GUEST_A)
        p.start()
        self.addCleanup(p.stop)

    def test_invite_ne_voit_que_ses_propres_notes(self):
        r = self.client.get("/feedback")
        self.assertEqual(r.status_code, 200)
        t = _text(r)
        self.assertNotIn("Tous les retours (tous les utilisateurs)", t)
        self.assertIn("note d'Alice", t)       # la sienne
        self.assertNotIn("note de Bob", t)     # celle d'un autre
        self.assertNotIn("note du propriétaire", t)


if __name__ == "__main__":
    unittest.main()
