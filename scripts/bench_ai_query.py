#!/usr/bin/env python3
"""Banc de mesure hors-ligne pour scripts/ai_query.py.

Vérifie la robustesse de la cascade de modèles et le traitement des erreurs
de l'API Gemini — aucun appel réseau, tout est mocké via urllib.request.urlopen.

12 cas couvrant :
- Repli de cascade sur erreur récupérable (429/404/5xx/timeout)
- Arrêt immédiat sur erreur non récupérable (400/401/403)
- Cascade entièrement épuisée
- Réponse vide (candidates=[]) et filtre de sécurité

Contrat : accepte --json et écrit sur stdout le verdict normalisé attendu par
scripts/loop/bench.py ({"script", "adversarial", "floor", "failed"}).

Usage : python3 scripts/bench_ai_query.py [-v] [--json]
"""
import argparse
import io
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from scripts.ai_query import (
    GeminiHTTPError,
    TIER_CASCADES,
    query_with_fallback,
    write_receipt,
    main as ai_query_main,
)


# ---------------------------------------------------------------------------
# Helpers de mock
# ---------------------------------------------------------------------------

def _ok_response(text="réponse ok", usage=None):
    """Réponse 200 avec du texte, prête pour urlopen en context manager."""
    payload = {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": usage or {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }
    cm = MagicMock()
    cm.read.return_value = json.dumps(payload).encode("utf-8")
    cm.__enter__.return_value = cm
    return cm


def _http_error(code, message="erreur"):
    body = json.dumps({"error": {"message": message}}).encode("utf-8")
    return urllib.error.HTTPError(
        "https://fake.googleapis.com", code, message, {}, io.BytesIO(body),
    )


def _empty_candidates_response():
    payload = {
        "candidates": [],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0,
                          "totalTokenCount": 10},
    }
    cm = MagicMock()
    cm.read.return_value = json.dumps(payload).encode("utf-8")
    cm.__enter__.return_value = cm
    return cm


def _safety_filtered_response():
    payload = {
        "candidates": [{
            "finishReason": "SAFETY",
            "content": {"parts": [{"text": ""}]},
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0,
                          "totalTokenCount": 10},
    }
    cm = MagicMock()
    cm.read.return_value = json.dumps(payload).encode("utf-8")
    cm.__enter__.return_value = cm
    return cm


# ---------------------------------------------------------------------------
# Définition des 12 cas
# ---------------------------------------------------------------------------

CASES = [
    # --- FLOOR : repli de cascade, comportement déjà correct ---
    {
        "id": "cascade_429_quota",
        "kind": "floor",
        "description": "429 quota épuisé → repli sur le modèle suivant",
    },
    {
        "id": "cascade_429_ratelimit",
        "kind": "floor",
        "description": "429 rate limit → repli sur le modèle suivant",
    },
    {
        "id": "cascade_404_modele_retire",
        "kind": "floor",
        "description": "404 modèle retiré → repli sur le modèle suivant",
    },
    {
        "id": "cascade_500_interne",
        "kind": "floor",
        "description": "500 erreur interne → repli sur le modèle suivant",
    },
    {
        "id": "cascade_502_passerelle",
        "kind": "floor",
        "description": "502 bad gateway → repli sur le modèle suivant",
    },
    {
        "id": "cascade_timeout",
        "kind": "floor",
        "description": "Timeout réseau (URLError) → repli sur le modèle suivant",
    },
    # --- ADVERSARIAL : cas limites, erreurs non récupérables, edge cases ---
    {
        "id": "arret_400_requete_invalide",
        "kind": "adversarial",
        "description": "400 requête invalide → arrêt immédiat, pas de repli",
    },
    {
        "id": "arret_401_auth",
        "kind": "adversarial",
        "description": "401 authentification → arrêt immédiat, pas de repli",
    },
    {
        "id": "arret_403_permission",
        "kind": "adversarial",
        "description": "403 permission refusée → arrêt immédiat, pas de repli",
    },
    {
        "id": "cascade_epuisee",
        "kind": "adversarial",
        "description": "Tous les modèles heavy en 429 → erreur explicite",
    },
    {
        "id": "reponse_vide_candidates",
        "kind": "adversarial",
        "description": "200 OK mais candidates=[] → texte vide remonté",
    },
    {
        "id": "filtre_securite",
        "kind": "adversarial",
        "description": "200 OK avec finishReason=SAFETY → texte vide remonté",
    },
    # --- ADVERSARIAL : reçus d'erreur enrichis (finding 1 du diagnostic 23/09) ---
    {
        "id": "recu_cascade_epuisee_error_type",
        "kind": "adversarial",
        "description": "Cascade épuisée → le reçu contient error_type='cascade_exhausted'",
    },
    {
        "id": "recu_reponse_vide_error_type",
        "kind": "adversarial",
        "description": "Réponse vide → le reçu contient error_type='empty_response'",
    },
    {
        "id": "recu_syntax_error_type",
        "kind": "adversarial",
        "description": "Code invalide → le reçu contient error_type='syntax_error'",
    },
]


# ---------------------------------------------------------------------------
# Exécution des cas
# ---------------------------------------------------------------------------

def _run_cascade_fallback(error_factory):
    """Cas de repli : le 1er modèle échoue, le 2e répond."""
    models = TIER_CASCADES["heavy"]
    call_count = {"n": 0}

    def side_effect(req, timeout=60):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise error_factory()
        return _ok_response()

    with patch("urllib.request.urlopen", side_effect=side_effect):
        text, model, usage = query_with_fallback(
            "test", tier="heavy", api_key="fake-key",
        )
    return text == "réponse ok" and model == models[1] and call_count["n"] == 2


def run_case(case):
    """Exécute un cas et renvoie (ok: bool, detail: str)."""
    cid = case["id"]

    if cid == "cascade_429_quota":
        ok = _run_cascade_fallback(
            lambda: _http_error(429, "Resource has been exhausted"))
        return ok, "repli après 429 quota"

    if cid == "cascade_429_ratelimit":
        ok = _run_cascade_fallback(
            lambda: _http_error(429, "rate limit exceeded"))
        return ok, "repli après 429 rate limit"

    if cid == "cascade_404_modele_retire":
        ok = _run_cascade_fallback(lambda: _http_error(404, "model not found"))
        return ok, "repli après 404"

    if cid == "cascade_500_interne":
        ok = _run_cascade_fallback(
            lambda: _http_error(500, "internal server error"))
        return ok, "repli après 500"

    if cid == "cascade_502_passerelle":
        ok = _run_cascade_fallback(lambda: _http_error(502, "bad gateway"))
        return ok, "repli après 502"

    if cid == "cascade_timeout":
        ok = _run_cascade_fallback(
            lambda: urllib.error.URLError("timed out"))
        return ok, "repli après timeout réseau"

    if cid == "arret_400_requete_invalide":
        call_count = {"n": 0}

        def side_effect(req, timeout=60):
            call_count["n"] += 1
            raise _http_error(400, "invalid request")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            try:
                query_with_fallback("test", tier="heavy", api_key="fake-key")
                return False, "pas d'exception levée"
            except GeminiHTTPError as e:
                ok = e.status == 400 and call_count["n"] == 1
                return ok, f"status={e.status}, appels={call_count['n']}"

    if cid == "arret_401_auth":
        call_count = {"n": 0}

        def side_effect(req, timeout=60):
            call_count["n"] += 1
            raise _http_error(401, "unauthenticated")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            try:
                query_with_fallback("test", tier="heavy", api_key="fake-key")
                return False, "pas d'exception levée"
            except GeminiHTTPError as e:
                ok = e.status == 401 and call_count["n"] == 1
                return ok, f"status={e.status}, appels={call_count['n']}"

    if cid == "arret_403_permission":
        call_count = {"n": 0}

        def side_effect(req, timeout=60):
            call_count["n"] += 1
            raise _http_error(403, "permission denied")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            try:
                query_with_fallback("test", tier="heavy", api_key="fake-key")
                return False, "pas d'exception levée"
            except GeminiHTTPError as e:
                ok = e.status == 403 and call_count["n"] == 1
                return ok, f"status={e.status}, appels={call_count['n']}"

    if cid == "cascade_epuisee":
        n_models = len(TIER_CASCADES["heavy"])
        call_count = {"n": 0}

        def side_effect(req, timeout=60):
            call_count["n"] += 1
            raise _http_error(429, "Resource has been exhausted")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            try:
                query_with_fallback("test", tier="heavy", api_key="fake-key")
                return False, "pas d'exception levée"
            except RuntimeError as e:
                ok = (call_count["n"] == n_models
                      and "cascade" in str(e).lower())
                return ok, f"RuntimeError après {call_count['n']}/{n_models} modèles"

    if cid == "reponse_vide_candidates":
        with patch("urllib.request.urlopen", return_value=_empty_candidates_response()):
            text, model, usage = query_with_fallback(
                "test", tier="heavy", api_key="fake-key",
            )
            ok = text == ""
            return ok, f"texte={'vide' if not text else repr(text)}"

    if cid == "filtre_securite":
        with patch("urllib.request.urlopen", return_value=_safety_filtered_response()):
            text, model, usage = query_with_fallback(
                "test", tier="heavy", api_key="fake-key",
            )
            ok = text == ""
            return ok, f"texte={'vide' if not text else repr(text)}"

    if cid == "recu_cascade_epuisee_error_type":
        receipt_calls = []
        orig_write = write_receipt.__wrapped__ if hasattr(write_receipt, "__wrapped__") else None

        def _capture_receipt(mode, status, **extra):
            receipt_calls.append({"mode": mode, "status": status, **extra})

        n_models = len(TIER_CASCADES["heavy"])
        call_count = {"n": 0}

        def side_effect(req, timeout=60):
            call_count["n"] += 1
            raise _http_error(429, "Resource has been exhausted")

        with (patch("urllib.request.urlopen", side_effect=side_effect),
              patch("scripts.ai_query.write_receipt", side_effect=_capture_receipt),
              patch("sys.argv", ["ai_query.py", "--mode", "code", "test prompt"])):
            ai_query_main()

        err_receipts = [r for r in receipt_calls if r.get("status") == "error"]
        if not err_receipts:
            return False, "aucun reçu d'erreur capturé"
        has_type = any(r.get("error_type") == "cascade_exhausted" for r in err_receipts)
        return has_type, f"error_type={'présent' if has_type else 'ABSENT'} dans le reçu"

    if cid == "recu_reponse_vide_error_type":
        receipt_calls = []

        def _capture_receipt(mode, status, **extra):
            receipt_calls.append({"mode": mode, "status": status, **extra})

        with (patch("urllib.request.urlopen", return_value=_empty_candidates_response()),
              patch("scripts.ai_query.write_receipt", side_effect=_capture_receipt),
              patch("sys.argv", ["ai_query.py", "--mode", "code", "test prompt"])):
            ai_query_main()

        err_receipts = [r for r in receipt_calls if r.get("status") == "error"]
        if not err_receipts:
            return False, "aucun reçu d'erreur capturé"
        has_type = any(r.get("error_type") == "empty_response" for r in err_receipts)
        return has_type, f"error_type={'présent' if has_type else 'ABSENT'} dans le reçu"

    if cid == "recu_syntax_error_type":
        receipt_calls = []

        def _capture_receipt(mode, status, **extra):
            receipt_calls.append({"mode": mode, "status": status, **extra})

        bad_code = "def f(\n  pass  # syntax error"
        with (patch("urllib.request.urlopen", return_value=_ok_response(text=bad_code)),
              patch("scripts.ai_query.write_receipt", side_effect=_capture_receipt),
              patch("sys.argv", ["ai_query.py", "--mode", "code", "--check-syntax",
                                 "test prompt"])):
            ai_query_main()

        err_receipts = [r for r in receipt_calls if r.get("status") == "error"]
        if not err_receipts:
            return False, "aucun reçu d'erreur capturé"
        has_type = any(r.get("error_type") == "syntax_error" for r in err_receipts)
        return has_type, f"error_type={'présent' if has_type else 'ABSENT'} dans le reçu"

    return False, f"cas inconnu : {cid}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", action="store_true",
                    help="détail de chaque cas, pas seulement les échecs")
    ap.add_argument("--json", action="store_true",
                    help="verdict normalisé sur stdout (contrat scripts/loop/bench.py)")
    args = ap.parse_args()

    results = []
    for case in CASES:
        try:
            ok, detail = run_case(case)
        except Exception as exc:
            ok, detail = False, f"exception inattendue : {exc}"
        results.append((case, ok, detail))

    n_ok = sum(1 for _, ok, _ in results if ok)

    if args.json:
        out = {"script": "ai_query", "failed": []}
        for kind in ("adversarial", "floor"):
            sub = [(c, ok) for c, ok, _ in results if c["kind"] == kind]
            out[kind] = {"ok": sum(1 for _, ok in sub if ok), "total": len(sub)}
            out["failed"] += [c["id"] for c, ok in sub if not ok]
        json.dump(out, sys.stdout)
        return 0 if n_ok == len(results) else 1

    for kind, libelle in (("adversarial", "cas adverses (discriminants)"),
                           ("floor", "plancher (non discriminant)")):
        sub = [(c, ok, d) for c, ok, d in results if c["kind"] == kind]
        if sub:
            k_ok = sum(1 for _, ok, _ in sub if ok)
            print(f"\n{libelle} : {k_ok}/{len(sub)}")
            for c, ok, d in sub:
                status = "OK  " if ok else "FAIL"
                if args.v or not ok:
                    print(f"  [{status}] {c['id']} — {d}")

    print(f"\n{n_ok}/{len(results)} cas passés ({100 * n_ok / len(results):.0f}%)")
    if n_ok < len(results):
        print("Échecs :", ", ".join(c["id"] for c, ok, _ in results if not ok))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
