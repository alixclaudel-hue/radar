import unittest
from scripts.ai.providers import HTTPStatusError, ProviderError


class TestHTTPStatusErrorProviderFatal(unittest.TestCase):
    """Tests unitaires pour la propriété provider_fatal de HTTPStatusError."""

    def test_403_avec_marqueur_agentic_harnesses_n_est_pas_fatal(self):
        """Cas (1) : 403 avec restriction modèle -> provider_fatal doit être False."""
        erreur = HTTPStatusError(
            "Model thinkingmachines/inkling-small:free is only available on agentic harnesses",
            status=403,
            provider="openrouter",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_403_avec_cle_invalide_est_fatal(self):
        """Cas (2) : 403 avec message d'authentification invalide -> provider_fatal doit être True."""
        erreur = HTTPStatusError(
            "Invalid API key provided",
            status=403,
            provider="openrouter",
        )
        self.assertTrue(erreur.provider_fatal)

    def test_401_sans_marqueur_restriction_est_fatal(self):
        """Cas (3) : 401 avec message quelconque sans restriction -> provider_fatal doit être True."""
        erreur = HTTPStatusError(
            "Unauthorized: token expired or invalid",
            status=401,
            provider="deepseek",
        )
        self.assertTrue(erreur.provider_fatal)

    def test_429_taux_limite_n_est_pas_fatal(self):
        """Cas (4) : 429 (rate limit) hors PROVIDER_FATAL_STATUSES -> provider_fatal doit être False."""
        erreur = HTTPStatusError(
            "Rate limit exceeded. Please try again later.",
            status=429,
            provider="gemini",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_403_avec_autre_marqueur_not_available_for_n_est_pas_fatal(self):
        """Cas limite : 403 avec l'autre marqueur 'not available for' -> provider_fatal doit être False."""
        erreur = HTTPStatusError(
            "This model is not available for free tier",
            status=403,
            provider="openrouter",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_403_insensible_a_la_casse(self):
        """Cas limite : la détection des marqueurs doit ignorer la casse (ex: MAJUSCULES)."""
        erreur = HTTPStatusError(
            "ERROR: IS ONLY AVAILABLE ON AGENTIC HARNESSES",
            status=403,
            provider="openrouter",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_status_none_n_est_pas_fatal(self):
        """Cas limite : status=None -> provider_fatal doit être False."""
        erreur = HTTPStatusError(
            "Erreur indéterminée",
            status=None,
            provider="test",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_status_zero_n_est_pas_fatal(self):
        """Cas limite : status=0 -> provider_fatal doit être False."""
        erreur = HTTPStatusError(
            "Status 0 inconnu",
            status=0,
            provider="test",
        )
        self.assertFalse(erreur.provider_fatal)

    def test_message_vide_avec_status_401_est_fatal(self):
        """Cas limite : message vide avec code 401 -> provider_fatal doit être True."""
        erreur = HTTPStatusError(
            "",
            status=401,
            provider="openrouter",
        )
        self.assertTrue(erreur.provider_fatal)

    def test_message_vide_avec_status_403_est_fatal(self):
        """Cas limite : message vide avec code 403 (sans marqueur) -> provider_fatal doit être True."""
        erreur = HTTPStatusError(
            "",
            status=403,
            provider="openrouter",
        )
        self.assertTrue(erreur.provider_fatal)

    def test_erreur_heritee_provider_error(self):
        """Cas de type : HTTPStatusError est bien une sous-classe de ProviderError."""
        erreur = HTTPStatusError("Test héritage", status=401)
        self.assertIsInstance(erreur, ProviderError)


if __name__ == '__main__':
    unittest.main()
