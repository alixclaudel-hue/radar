"""Non-régression du bug du 2026-09-27 : la saisie des identifiants Subsonic
Bandcamp affichait « JSONDecodeError: Expecting value: line 1 column 1 (char 0) ».

Cause : Bandcamp protège `bandcamp.com/api/...` par un « Client Challenge »
Fastly. Depuis une IP de datacenter, la réponse est un 200 `text/html` et
`r.json()` levait l'erreur brute, remontée telle quelle dans le suivi du job.
`subsonic_get` doit désormais lever un RuntimeError explicite, jamais un
JSONDecodeError.
"""
import unittest
from unittest import mock

from radar_jobs import sources


class _FakeResp:
    def __init__(self, text="", ctype="text/html; charset=utf-8", status=200):
        self.text = text
        self.status_code = status
        self.ok = status < 400
        self.headers = {"content-type": ctype}

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _ChallengeResp(_FakeResp):
    pass


class _JsonResp(_FakeResp):
    def __init__(self, payload, status=200):
        super().__init__("", "application/json", status)
        self._payload = payload

    def json(self):
        return self._payload


CHALLENGE_HTML = "<!DOCTYPE html><html><head><title>Client Challenge</title></head></html>"


class TestSubsonicGet(unittest.TestCase):

    def test_challenge_html_leve_un_message_explicite_sans_jsondecodeerror(self):
        with mock.patch.object(sources.requests, "get",
                               return_value=_ChallengeResp(CHALLENGE_HTML)):
            with self.assertRaises(RuntimeError) as cm:
                sources.subsonic_get("getAlbumList2", "u", "p")
        msg = str(cm.exception)
        self.assertIn("anti-bot", msg)
        self.assertIn("getAlbumList2", msg)

    def test_reponse_html_sans_status_ok(self):
        # 200 text/html trompeur : c'est bien le contenu qui tranche, pas le code
        with mock.patch.object(sources.requests, "get",
                               return_value=_ChallengeResp("<!doctype html>")):
            self.assertRaises(RuntimeError, sources.subsonic_get, "ping", "u", "p")

    def test_json_valide_ok(self):
        payload = {"subsonic-response": {"status": "ok", "albumList2": {"album": []}}}
        with mock.patch.object(sources.requests, "get", return_value=_JsonResp(payload)):
            body = sources.subsonic_get("getAlbumList2", "u", "p")
        self.assertEqual(body["status"], "ok")

    def test_json_statut_failed_remonte_le_message(self):
        payload = {"subsonic-response": {"status": "failed",
                                         "error": {"message": "Wrong username or password"}}}
        with mock.patch.object(sources.requests, "get", return_value=_JsonResp(payload)):
            with self.assertRaises(RuntimeError) as cm:
                sources.subsonic_get("getAlbumList2", "u", "p")
        self.assertIn("Wrong username or password", str(cm.exception))

    def test_json_illisible_est_encapsule(self):
        with mock.patch.object(sources.requests, "get",
                               return_value=_FakeResp("pas du json", "application/json")):
            with self.assertRaises(RuntimeError) as cm:
                sources.subsonic_get("getAlbumList2", "u", "p")
        self.assertIn("illisible", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
