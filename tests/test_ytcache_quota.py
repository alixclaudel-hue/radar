"""Cas de classification quota/rate-limit YouTube (`ytcache._error_kind` et
`ytcache.request`) -- la frontière a basculé deux fois en 24h (16/09, cf.
CLAUDE.md pts 51-52) sans qu'aucun de ces cas ne soit committé nulle part
(reproché par la session VPS lors de la 2e vérif). Au minimum les 5 cas
prouvés en conditions réelles ce jour-là sont couverts ici ; le reste
verrouille la non-régression des deux sens du bug (marqueur trop large ->
un rate-limit transitoire classé "daily" à tort ; marqueur absent -> un
quota journalier classé "rate" à tort).

Lancer : python3 -m unittest tests.test_ytcache_quota -v
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import ytcache  # noqa: E402


class FakeResponse:
    """Imite juste assez `requests.Response` pour `_error_kind`/`_reason`/
    `_error_message` : `.status_code`, `.json()`, `.text`."""

    def __init__(self, status_code, reasons=(), message="", raw_text=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._reasons = list(reasons)
        self._message = message
        self._raw_text = raw_text

    def _body(self):
        return {"error": {"message": self._message,
                           "errors": [{"reason": r} for r in self._reasons]}}

    def json(self):
        if self._raw_text is not None:
            raise ValueError("réponse non-JSON")
        return self._body()

    @property
    def text(self):
        if self._raw_text is not None:
            return self._raw_text
        return json.dumps(self._body())


class ErrorKindRealCases(unittest.TestCase):
    """Les 5 cas rejoués à la main par la session VPS le 16/09 (18h Paris,
    contre la vraie API, avec une vraie clé) -- doivent rester classés
    exactement ainsi."""

    def test_quota_exceeded_classique(self):
        r = FakeResponse(403, reasons=["quotaExceeded"], message="Quota exceeded")
        self.assertEqual(ytcache._error_kind(r), "daily")

    def test_rate_limit_exceeded_avec_per_day_dans_le_message(self):
        # Cas réel exact : reason ment ("rateLimitExceeded"), le message
        # nomme la métrique "Search Queries per day".
        r = FakeResponse(
            429, reasons=["rateLimitExceeded"],
            message="Quota exceeded for quota metric 'Search Queries' and "
                    "limit 'Search Queries per day' of service "
                    "'youtube.googleapis.com' for consumer "
                    "'project_number:778048350877'")
        self.assertEqual(ytcache._error_kind(r), "daily")

    def test_rate_limit_exceeded_nu(self):
        r = FakeResponse(429, reasons=["rateLimitExceeded"],
                          message="User Rate Limit Exceeded")
        self.assertEqual(ytcache._error_kind(r), "rate")

    def test_queries_per_minute_per_user(self):
        r = FakeResponse(
            429, reasons=["rateLimitExceeded"],
            message="Quota exceeded for quota metric 'Queries' and limit "
                    "'Queries per minute per user' of service "
                    "'youtube.googleapis.com'")
        self.assertEqual(ytcache._error_kind(r), "rate")

    def test_429_sans_reason_avec_quota_dans_le_texte(self):
        r = FakeResponse(429, reasons=[], raw_text="Quota Queries per day exceeded")
        self.assertEqual(ytcache._error_kind(r), "daily")


class ErrorKindNonRegression(unittest.TestCase):
    """Verrouille les deux sens du bug (16/09) : un marqueur trop large classe
    un rate-limit transitoire en "daily" à tort ; un marqueur absent classe un
    quota journalier en "rate" à tort. Plus quelques cas hors quota."""

    def test_daily_limit_exceeded_reason_classique(self):
        r = FakeResponse(403, reasons=["dailyLimitExceeded"], message="Daily Limit Exceeded")
        self.assertEqual(ytcache._error_kind(r), "daily")

    def test_user_rate_limit_exceeded_avec_per_day(self):
        r = FakeResponse(429, reasons=["userRateLimitExceeded"],
                          message="limit 'Search Queries per day' exceeded")
        self.assertEqual(ytcache._error_kind(r), "daily")

    def test_casse_mixte_search_queries_per_day(self):
        r = FakeResponse(429, reasons=["rateLimitExceeded"],
                          message="limit 'Search Queries Per Day' exceeded")
        self.assertEqual(ytcache._error_kind(r), "daily")

    def test_per_100_seconds_reste_rate(self):
        r = FakeResponse(429, reasons=["rateLimitExceeded"],
                          message="limit 'Queries per 100 seconds per user' exceeded")
        self.assertEqual(ytcache._error_kind(r), "rate")

    def test_403_forbidden_cle_invalide_nest_pas_un_quota(self):
        r = FakeResponse(403, reasons=["forbidden"], message="Forbidden")
        self.assertIsNone(ytcache._error_kind(r))

    def test_500_nest_jamais_un_quota(self):
        r = FakeResponse(500, reasons=["backendError"], message="Internal error")
        self.assertIsNone(ytcache._error_kind(r))

    def test_reponse_non_json_sans_quota_dans_le_texte(self):
        r = FakeResponse(429, raw_text="upstream connect error")
        self.assertIsNone(ytcache._error_kind(r))

    def test_reponse_non_json_avec_quota_dans_le_texte(self):
        r = FakeResponse(429, raw_text="quota exceeded, retry later")
        self.assertEqual(ytcache._error_kind(r), "daily")


class RequestMultiKeyOrdering(unittest.TestCase):
    """`request()` doit conclure `RateLimited` (pas `QuotaExhausted`) dès
    qu'AU MOINS une clé n'est que rate-limited, même si une autre clé est
    réellement épuisée pour la journée -- sans ça (bug trouvé en relecture
    avant la PR #159), 2 clés dont une seulement transitoire faisait perdre
    toute la journée à tort. Sans effet à une seule clé, actif dès 2."""

    def _patched(self, responses_by_key):
        def fake_get(path, params, key, timeout):
            return responses_by_key[key]
        return mock.patch.object(ytcache, "_get", side_effect=fake_get)

    def test_une_cle_rate_une_cle_daily_donne_ratelimited(self):
        responses = {
            "k1": FakeResponse(429, reasons=["rateLimitExceeded"], message="User Rate Limit"),
            "k2": FakeResponse(403, reasons=["quotaExceeded"], message="Quota exceeded"),
        }
        with self._patched(responses):
            with self.assertRaises(ytcache.RateLimited):
                ytcache.request("/search", {}, ["k1", "k2"])

    def test_deux_cles_daily_donne_quotaexhausted(self):
        responses = {
            "k1": FakeResponse(403, reasons=["quotaExceeded"], message="Quota exceeded"),
            "k2": FakeResponse(403, reasons=["quotaExceeded"], message="Quota exceeded"),
        }
        with self._patched(responses):
            with self.assertRaises(ytcache.QuotaExhausted):
                ytcache.request("/search", {}, ["k1", "k2"])

    def test_une_seule_cle_rate_donne_ratelimited_pas_quotaexhausted(self):
        responses = {"k1": FakeResponse(429, reasons=["rateLimitExceeded"], message="User Rate Limit")}
        with self._patched(responses):
            with self.assertRaises(ytcache.RateLimited):
                ytcache.request("/search", {}, ["k1"])

    def test_erreur_non_classee_leve_runtimeerror(self):
        # Cas atteignable en réel (clé invalide/révoquée -> reason=forbidden) :
        # remonte tel quel, non rattrapé par QuotaExhausted/RateLimited -- ce
        # que job_publish_recos doit maintenant survivre sans perdre l'état du
        # run (cf. tests/test_job_publish_recos.py, BUG 1).
        responses = {"k1": FakeResponse(403, reasons=["forbidden"], message="Forbidden")}
        with self._patched(responses):
            with self.assertRaises(RuntimeError):
                ytcache.request("/search", {}, ["k1"])

    def test_succes_renvoie_le_json(self):
        ok = FakeResponse(200)
        ok.ok = True
        ok.json = lambda: {"items": []}
        with self._patched({"k1": ok}):
            self.assertEqual(ytcache.request("/search", {}, ["k1"]), {"items": []})


if __name__ == "__main__":
    unittest.main()
