#!/usr/bin/env python3
"""Banc de mesure hors-ligne pour scripts/ai_broker.py.

Vérifie la passerelle multi-fournisseurs : routage gratuit d'abord, repli sur le
candidat suivant, rotation de clés, quota par fenêtre, disjoncteur par modèle,
plafond payant, masquage des secrets, enveloppe JSON des modes de code, cache
disque du mode `context`, recherche locale du mode `search` et traduction du
budget de réflexion (`--effort`) dans la forme propre de chaque fournisseur —
**aucun appel réseau**, aucun fichier d'état réel touché (chaque cas pointe `RADAR_AI_USAGE_DIR`
vers un dossier temporaire, ce qui isole du même coup l'index de contexte qui vit
dessous).

Le banc n'est pas une redite des tests unitaires : il exerce `main()` de bout en
bout, avec un catalogue écrit sur disque et l'état lu depuis l'environnement.
C'est le seul endroit qui prouve que l'ordre des candidats, la persistance du
quota et la forme du reçu tiennent ensemble.

Contrat : accepte `--json` et écrit sur stdout le verdict normalisé attendu par
`scripts/loop/bench.py` (`{"script", "adversarial", "floor", "failed"}`).

Deux cas ont été trouvés par ce banc puis corrigés ; ils restent comme garde-fous
du défaut qu'ils ont mesuré :

- `transcribe_non_conversationnel` — un modèle de transcription n'a rien à faire
  au catalogue textuel, où il serait proposé pour du code.
- `sante_sur_403_punit_le_modele` — un 401/403 est une faute de clé : il ne doit
  pas écarter un modèle sain, ce que `health` réserve au ledger de clés.

La garantie de sortie JSON (plan §6, mécanisme 3) a ses propres cas : le contrat
strict part en format **natif** quand le catalogue déclare `structured_outputs`,
retombe sur `json_object` sinon, et un dérapage de format se rattrape en **une**
relance avant l'échec explicite — jamais de JSON à moitié valide rendu à l'appelant.

Usage : python3 scripts/bench_ai_broker.py [-v] [--json]
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from scripts import ai_broker, ai_query  # noqa: E402
from scripts.ai import catalogue as cat_mod  # noqa: E402
from scripts.ai import (  # noqa: E402
    context_cache,
    health,
    providers,
    quota,
    refresh_models,
    schema,
)

# Dossiers temporaires créés par les cas : nettoyés une seule fois, à la fin de
# `main()`, pour qu'un cas qui lève ne laisse pas un dossier derrière lui.
_TEMP_DIRS: list[str] = []

# Clés fictives des catalogues de banc. Elles ne ressemblent à aucune clé réelle
# et ne sont jamais écrites sur disque : `providers.env_value` est remplacé.
_CLES_STD = {
    "OPENROUTER_API_KEY": "banc-or-1",
    "GEMINI_API_KEY": "banc-gm-1",
}
_CLES_PAYANTES = {"DEEPSEEK_API_KEY": "banc-ds-1"}


# ---------------------------------------------------------------------------
# Construction de catalogues
# ---------------------------------------------------------------------------

def _model(
    model_id,
    *,
    free=True,
    rank=100,
    context=None,
    prompt_per_1m=0.0,
    completion_per_1m=0.0,
    supports=None,
    paid_ok=False,
):
    """Une entrée de modèle.

    Piège central : `catalogue.is_free()` rend `True` dès que les **deux** prix
    sont nuls. Un modèle payant de banc doit donc porter au moins un prix non
    nul, sinon il serait routé comme gratuit et le cas ne testerait rien.
    """
    entry = {"id": model_id, "rank": rank}
    if free:
        entry["free"] = True
        entry["pricing"] = {"prompt_per_1m": 0.0, "completion_per_1m": 0.0}
    else:
        entry["pricing"] = {
            "prompt_per_1m": prompt_per_1m,
            "completion_per_1m": completion_per_1m,
        }
    if context is not None:
        entry["context_length"] = context
    if supports is not None:
        entry["supports"] = list(supports)
    if paid_ok:
        entry["paid_ok"] = True
    return entry


def _provider(
    name,
    *,
    kind="openai",
    base_url="https://exemple.test/v1",
    keys_env=None,
    quota_spec=None,
    models=None,
    reasoning_style="",
    reasoning_map=None,
    headers=None,
):
    bloc = {"kind": kind, "base_url": base_url, "models": list(models or [])}
    if keys_env is not None:
        bloc["keys_env"] = list(keys_env)
    if quota_spec is not None:
        bloc["quota"] = quota_spec
    if reasoning_style:
        bloc["reasoning_style"] = reasoning_style
    if reasoning_map:
        bloc["reasoning_map"] = dict(reasoning_map)
    if headers:
        bloc["headers"] = dict(headers)
    return bloc


def _catalogue(providers_map, policy=None):
    """Catalogue minimal. L'ordre des clés de `providers_map` est l'ordre de
    préférence du routeur : il est donc significatif dans chaque cas."""
    cat = cat_mod.empty_catalogue()
    cat["providers"] = providers_map
    if policy:
        cat["policy"] = {**cat["policy"], **policy}
    return cat


def _std_catalogue(policy=None):
    """Deux fournisseurs gratuits : OpenRouter (deux modèles) puis Gemini."""
    return _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                quota_spec={"window": "daily", "limit": 50},
                models=[
                    _model("or-free-1", rank=10),
                    _model("or-free-2", rank=20),
                ],
            ),
            "gm": _provider(
                "gm",
                kind="gemini",
                base_url="",
                keys_env=["GEMINI_API_KEY"],
                models=[_model("gm-free-1", rank=10)],
            ),
        },
        policy,
    )


def _paid_catalogue(*, paid_ok=True, prix=(10.0, 10.0), policy=None):
    return _catalogue(
        {
            "ds": _provider(
                "ds",
                base_url="https://api.deepseek.test/v1",
                keys_env=["DEEPSEEK_API_KEY"],
                quota_spec={"window": "monthly", "limit": None},
                models=[
                    _model(
                        "ds-paid",
                        free=False,
                        rank=10,
                        prompt_per_1m=prix[0],
                        completion_per_1m=prix[1],
                        paid_ok=paid_ok,
                    )
                ],
            )
        },
        policy,
    )


def _seul_gratuit(*, limit=None, keys_env=None, model_id="or-free-1", policy=None):
    """Un seul fournisseur gratuit, pour isoler un refus de clé ou de quota."""
    return _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=keys_env or ["OPENROUTER_API_KEY"],
                quota_spec={"window": "daily", "limit": limit},
                models=[_model(model_id)],
            )
        },
        policy,
    )


# ---------------------------------------------------------------------------
# Bouchons : réponses, erreurs, états pré-remplis
# ---------------------------------------------------------------------------

def _completion(model, provider, text="réponse ok", usage=None):
    return providers.Completion(
        text=text,
        model=model,
        provider=provider,
        usage=usage
        or {"prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def _http(status, *, retry_after=None, provider="or"):
    return providers.HTTPStatusError(
        f"{provider} HTTP {status} : erreur de banc",
        status=status,
        retry_after=retry_after,
        provider=provider,
    )


def _reseau(provider="or"):
    return providers.NetworkError(f"réseau {provider} : panne de banc", provider=provider)


def _lookup(mapping):
    def _cherche(name):
        return mapping.get(name)

    return _cherche


def _fake_chat(plan, appels):
    """Remplace `chat()` des deux adaptateurs par une séquence scriptée.

    `plan` est consommé dans l'ordre des appels : `None` = réponse correcte,
    chaîne = texte de réponse, `BaseException` = erreur levée. Le dernier élément
    est répété au-delà, ce qui rend les cas à repli lisibles sans compter les
    essais à la main.
    """
    def _chat(self, model, prompt, system="", **kwargs):
        nom = getattr(getattr(self, "spec", None), "name", "?")
        index = min(len(appels), max(0, len(plan) - 1))
        appels.append(
            {
                "provider": nom,
                "model": model,
                "prompt": prompt,
                "system": system,
                **kwargs,
            }
        )
        if not plan:
            return _completion(model, nom)
        issue = plan[index]
        if issue is None:
            return _completion(model, nom)
        if isinstance(issue, BaseException):
            raise issue
        if isinstance(issue, str):
            return _completion(model, nom, text=issue)
        if isinstance(issue, dict):
            return _completion(
                model,
                nom,
                text=issue.get("text", "réponse ok"),
                usage=issue.get("usage"),
            )
        return issue

    return _chat


def _capture_urlopen(appels, reponse=None):
    """Laisse l'adaptateur construire sa vraie requête et capture son corps.

    C'est le seul moyen de prouver que le masquage a lieu **avant** la
    sérialisation, et non dans une couche que le banc aurait pu contourner.
    """
    charge = reponse or {
        "choices": [{"message": {"content": "réponse ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    def _urlopen(req, timeout=None):
        corps = json.loads(req.data.decode("utf-8")) if req.data else {}
        appels.append(
            {"url": req.full_url, "headers": dict(req.headers), "payload": corps}
        )
        cm = MagicMock()
        cm.read.return_value = json.dumps(charge).encode("utf-8")
        cm.__enter__.return_value = cm
        return cm

    return _urlopen


def _capture_receipt(recus):
    def _ecrit(mode, status, **extra):
        recus.append({"mode": mode, "status": status, **extra})

    return _ecrit


class _FauxStdin:
    """Entrée standard inerte : le banc ne doit pas dépendre du terminal."""

    def __init__(self, texte=""):
        self._texte = texte

    def isatty(self):
        return True

    def read(self):
        return self._texte


def _etat_cle(provider, key_id, *, window, count, bad_until=None):
    entree = {
        "window": quota.window_id(window),
        "count": count,
        "provider": provider,
        "key_id": key_id,
    }
    if bad_until is not None:
        entree["bad_until"] = bad_until
    return {
        "keys": {quota._key_slot(provider, key_id): entree},
        "paid": {"window": "", "spent_usd": 0.0},
        "updated": 0.0,
    }


def _etat_sante(provider, model, **champs):
    return {"entries": {health.key(provider, model): dict(champs)}, "updated": 0.0}


def _ecrire_json(path, donnees):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False)


def _message_utilisateur(payload):
    for message in reversed(payload.get("messages") or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


# ---------------------------------------------------------------------------
# Exécution du courtier, hors ligne
# ---------------------------------------------------------------------------

def _run_broker(
    argv,
    *,
    catalogue,
    plan=None,
    keys=None,
    quota_state=None,
    health_state=None,
    env=None,
    usage_dir=None,
    prompt="hello",
    stdin_text="",
    output_name=None,
):
    """Exécute `ai_broker.main()` sur un catalogue et un état jetables.

    `plan` non nul remplace `chat()` des deux adaptateurs (cas de routage) ;
    `plan` nul laisse le vrai `chat()` et remplace `urllib.request.urlopen`
    (cas de masquage et de charge utile).

    Rend tout ce qu'un cas peut vouloir observer : code de retour, appels émis,
    reçus écrits, sorties capturées, journal et états relus **depuis le disque**.
    """
    usage_dir = usage_dir or tempfile.mkdtemp(prefix="banc-broker-")
    os.makedirs(usage_dir, exist_ok=True)
    _TEMP_DIRS.append(usage_dir)

    cat_path = os.path.join(usage_dir, "catalogue.json")
    _ecrire_json(cat_path, catalogue)
    if quota_state is not None:
        _ecrire_json(os.path.join(usage_dir, quota.STATE_NAME), quota_state)
    if health_state is not None:
        _ecrire_json(os.path.join(usage_dir, health.STATE_NAME), health_state)

    env_final = {
        "RADAR_AI_USAGE_DIR": usage_dir,
        # Vide = « non déclaré » : le plafond vient alors du catalogue, donc
        # 0,00 $ par défaut, et aucun appel payant ne peut partir sans le dire.
        "RADAR_AI_DAILY_PAID_CAP_USD": "",
    }
    env_final.update(env or {})

    argv_final = ["--no-sleep", "--source", "banc", "--catalogue", cat_path]
    argv_final += list(argv)
    output_path = None
    if output_name:
        output_path = os.path.join(usage_dir, output_name)
        argv_final += ["-o", output_path]
    argv_final.append(prompt)

    appels: list[dict] = []
    recus: list[dict] = []
    sortie, sortie_err = io.StringIO(), io.StringIO()

    with contextlib.ExitStack() as pile:
        pile.enter_context(patch.dict(os.environ, env_final))
        pile.enter_context(patch.object(providers, "env_value", _lookup(keys or {})))
        pile.enter_context(
            patch.object(ai_query, "write_receipt", _capture_receipt(recus))
        )
        pile.enter_context(patch.object(sys, "stdin", _FauxStdin(stdin_text)))
        pile.enter_context(contextlib.redirect_stdout(sortie))
        pile.enter_context(contextlib.redirect_stderr(sortie_err))
        if plan is not None:
            fausse = _fake_chat(plan, appels)
            pile.enter_context(
                patch.object(providers.OpenAICompatProvider, "chat", fausse)
            )
            pile.enter_context(patch.object(providers.GeminiProvider, "chat", fausse))
        else:
            pile.enter_context(
                patch.object(urllib.request, "urlopen", _capture_urlopen(appels))
            )
        code = ai_broker.main(argv_final)

    return {
        "code": code,
        "appels": appels,
        "recus": recus,
        "stdout": sortie.getvalue(),
        "stderr": sortie_err.getvalue(),
        "ledger": quota.read_ledger(os.path.join(usage_dir, quota.LEDGER_NAME)),
        "quota_state": quota.load_state(os.path.join(usage_dir, quota.STATE_NAME)),
        "health_state": health.load(os.path.join(usage_dir, health.STATE_NAME)),
        "usage_dir": usage_dir,
        "output_path": output_path,
    }


def _cooldown_restant(res, provider, model):
    entree = (res["health_state"].get("entries") or {}).get(health.key(provider, model))
    if not isinstance(entree, dict):
        return None
    try:
        return float(entree.get("cooldown_until") or 0.0) - time.time()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cas
# ---------------------------------------------------------------------------

def _cas_succes_premier_candidat():
    res = _run_broker([], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[None])
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if not res["appels"] or res["appels"][0]["model"] != "or-free-1":
        return False, f"premier appel={res['appels'][0]['model'] if res['appels'] else 'aucun'}"
    if len(res["appels"]) != 1:
        return False, f"{len(res['appels'])} appels au lieu d'un"
    if len(res["ledger"]) != 1 or res["ledger"][0].get("status") != "ok":
        return False, f"journal={res['ledger']}"
    if res["ledger"][0].get("free") is not True:
        return False, "ligne de journal non marquée gratuite"
    if (res["health_state"].get("entries") or {}):
        return False, "un succès doit effacer l'entrée de santé"
    if res["stdout"] != "réponse ok\n":
        return False, f"stdout={res['stdout']!r}"
    return True, "premier candidat gratuit servi, journal et santé cohérents"


def _cas_gratuit_avant_payant():
    """Le payant est déclaré **en premier** et mieux classé : il doit passer après."""
    cat = _catalogue(
        {
            "ds": _provider(
                "ds",
                base_url="https://api.deepseek.test/v1",
                keys_env=["DEEPSEEK_API_KEY"],
                models=[
                    _model(
                        "ds-paid",
                        free=False,
                        rank=1,
                        prompt_per_1m=10.0,
                        completion_per_1m=10.0,
                        paid_ok=True,
                    )
                ],
            ),
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                models=[_model("or-free-1", rank=500)],
            ),
        }
    )
    # `explain()` publie les entrées **dans l'ordre du catalogue**, pas dans
    # l'ordre de tentative : la preuve de l'ordre se lit donc sur l'appel réel.
    options = {"keys": {**_CLES_STD, **_CLES_PAYANTES},
               "env": {"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"}}
    expose = _run_broker(["--list-candidates", "--allow-paid"], catalogue=cat, **options)
    if expose["code"] != 0:
        return False, f"code={expose['code']} stderr={expose['stderr'].strip()!r}"
    incluses = [l for l in expose["stdout"].splitlines() if l.startswith("[inclus]")]
    if len(incluses) != 2:
        return False, f"{len(incluses)} candidats inclus : {incluses}"

    res = _run_broker(["--allow-paid"], catalogue=cat, plan=[None], **options)
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, (
            f"{len(res['appels'])} appels : le payant a été sollicité alors qu'un "
            "gratuit était disponible"
        )
    if res["appels"][0]["model"] != "or-free-1":
        return False, f"premier appel={res['appels'][0]['model']!r}"
    return True, "gratuit appelé avant le payant, malgré un rang bien meilleur"


def _cas_repli_sur_400():
    """400 : la requête ne convient pas à ce modèle, un autre peut l'accepter."""
    res = _run_broker(
        [], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[_http(400), None]
    )
    if res["code"] != 0 or len(res["appels"]) != 2:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if res["appels"][1]["model"] != "or-free-2":
        return False, f"repli sur {res['appels'][1]['model']}"
    entree = (res["health_state"].get("entries") or {}).get(health.key("or", "or-free-1"))
    if not isinstance(entree, dict):
        return False, "échec 400 non enregistré dans la santé"
    if entree.get("cooldown_until") or entree.get("retired"):
        return False, f"un 400 ne doit poser ni cooldown ni retrait : {entree}"
    return True, "repli sur le modèle suivant, sans cooldown pour un 400"


def _cas_repli_apres_429():
    """429 : le même modèle est retenté (`per_model`), puis on passe au suivant."""
    res = _run_broker(
        [],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        plan=[_http(429), _http(429), None],
    )
    if res["code"] != 0 or len(res["appels"]) != 3:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if res["appels"][2]["model"] != "or-free-2":
        return False, f"repli sur {res['appels'][2]['model']}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 2 or any(l.get("error_type") != "http_429" for l in erreurs):
        return False, f"journal d'erreur={erreurs}"
    restant = _cooldown_restant(res, "or", "or-free-1")
    if restant is None or not 200.0 < restant < 300.0:
        return False, f"cooldown={restant} (attendu ~240 s : 120 × 2^(2-1))"
    return True, "deux essais sur le même modèle puis repli, cooldown doublé"


def _cas_retry_after_honore():
    """`Retry-After` prime sur le backoff : 7 s annoncées donnent ~8 s d'attente."""
    res = _run_broker(
        [],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        plan=[
            _http(429, retry_after=7),
            _http(429, retry_after=7),
            None,
        ],
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    restant = _cooldown_restant(res, "or", "or-free-1")
    if restant is None:
        return False, "aucun cooldown enregistré"
    if not 6.0 < restant < 10.0:
        return False, (
            f"cooldown={restant:.1f}s ; 240 s signifierait que `Retry-After` "
            "est ignoré"
        )
    return True, f"Retry-After honoré ({restant:.1f}s pour 7 s annoncées)"


def _cas_modele_explicite():
    res = _run_broker(
        ["-m", "or-free-2"],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        plan=[None],
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if res["appels"][0]["model"] != "or-free-2":
        return False, f"modèle appelé={res['appels'][0]['model']}"
    return True, "un modèle explicite court-circuite le routage"


def _ecart_quota(recu: dict, attendu: dict) -> str:
    """Écart entre le bloc `quota` d'un reçu et ce qui est attendu ("" = conforme).

    Le bloc est produit par **une seule** fonction, partagée par les chemins de
    succès, d'échec et de service sans appel. Le vérifier de la même façon partout
    est ce qui rend deux reçus comparables entre eux, au lieu d'être seulement
    lisibles un par un — c'est toute la différence entre un journal et un tableau
    de bord.
    """
    bloc = recu.get("quota")
    if not isinstance(bloc, dict):
        return f"bloc quota absent ou illisible : {bloc!r}"
    for champ, valeur in attendu.items():
        if bloc.get(champ) != valeur:
            return f"quota.{champ}={bloc.get(champ)!r} (attendu {valeur!r})"
    return ""


def _cas_recu_et_journal():
    res = _run_broker([], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[None])
    if len(res["recus"]) != 1:
        return False, f"{len(res['recus'])} reçus"
    recu = res["recus"][0]
    if recu.get("status") != "ok" or recu.get("mode") != "general":
        return False, f"reçu={recu}"
    requis = {
        "provider",
        "model",
        "tier",
        "prompt_chars",
        "attempts",
        "fallbacks",
        "key_id",
        "paid",
        "cost_usd",
        "source",
        "elapsed_ms",
        "routing_reason",
        # Jetons et quota restant : sans eux, un reçu dit qu'un appel est parti
        # mais pas ce qu'il a consommé du palier gratuit. C'est pourtant la seule
        # question à laquelle le tableau de bord doit répondre — reste-t-il du
        # gratuit, et pour combien de temps.
        "tokens",
        "quota",
    }
    absents = sorted(requis - set(recu))
    if absents:
        return False, f"champs absents du reçu : {absents}"
    if recu.get("source") != "banc":
        return False, f"source={recu.get('source')!r}"
    if recu.get("routing_reason") != "gratuit retenu":
        return False, f"motif de routage={recu.get('routing_reason')!r}"
    if not isinstance(recu.get("tokens"), dict) or not recu["tokens"]:
        return False, (
            f"jetons du reçu={recu.get('tokens')!r} : le reçu doit porter les jetons "
            "dans la même forme que la ligne de journal, sinon les deux ne se "
            "comparent pas"
        )
    ecart = _ecart_quota(
        recu, {"window": "daily", "limit": 50, "used": 1, "remaining": 49}
    )
    if ecart:
        return False, (
            f"{ecart} — un appel gratuit est parti : le reçu doit dire ce qui "
            "reste dans la fenêtre, pas seulement qu'il est parti"
        )
    ecart = _ecart_quota(recu, {"paid_cap_usd": 0.0, "paid_spent_usd": 0.0})
    if ecart:
        return False, (
            f"{ecart} — aucun appel payant n'est parti : le plafond du jour est "
            "donc celui du catalogue, et la dépense nulle"
        )
    if len(res["ledger"]) != 1 or res["ledger"][0].get("source") != "banc":
        return False, f"journal={res['ledger']}"
    if not isinstance(res["ledger"][0].get("tokens"), dict):
        return False, (
            f"ligne de journal={res['ledger'][0]} : les jetons doivent s'y lire "
            "sans avoir à relire le reçu"
        )
    return True, "un reçu complet et une ligne de journal par appel réussi"


def _cas_liste_candidats_sans_appel():
    res = _run_broker(
        ["--list-candidates"], catalogue=_std_catalogue(), keys=_CLES_STD
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if res["appels"] or res["recus"] or res["ledger"]:
        return False, (
            f"`--list-candidates` a eu des effets : {len(res['appels'])} appels, "
            f"{len(res['recus'])} reçus, {len(res['ledger'])} lignes"
        )
    lignes = res["stdout"].splitlines()
    if not lignes or not lignes[0].startswith("[inclus] or:or-free-1"):
        return False, f"première ligne={lignes[0] if lignes else 'aucune'!r}"
    return True, "routage exposé, aucun appel émis, aucun reçu écrit"


def _cas_output_ecrit_le_fichier():
    res = _run_broker(
        ["--raw-code"],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        plan=["```python\nx = 1\n```"],
        output_name="genere.py",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if not res["output_path"] or not os.path.isfile(res["output_path"]):
        return False, "fichier de sortie absent"
    with open(res["output_path"], "r", encoding="utf-8") as f:
        contenu = f.read()
    if contenu != "x = 1\n":
        return False, f"contenu={contenu!r}"
    if "Écrit dans" not in res["stderr"]:
        return False, f"pas de trace d'écriture : {res['stderr']!r}"
    if res["stdout"]:
        return False, f"stdout non vide alors que `-o` était fourni : {res['stdout']!r}"
    return True, "le bloc Python est écrit seul, sans clôture markdown"


def _cas_rotation_deux_cles():
    cat = _seul_gratuit(keys_env=["OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"])
    cles = {"OPENROUTER_API_KEY": "k1", "OPENROUTER_API_KEY_2": "k2"}
    dossier = tempfile.mkdtemp(prefix="banc-broker-rotation-")
    _TEMP_DIRS.append(dossier)
    premier = _run_broker([], catalogue=cat, keys=cles, plan=[None], usage_dir=dossier)
    second = _run_broker([], catalogue=cat, keys=cles, plan=[None], usage_dir=dossier)
    if not premier["ledger"] or not second["ledger"]:
        return False, "journal incomplet"
    # Le journal est relu **en entier** depuis le disque : dans le second
    # passage, la ligne du premier est donc encore là et c'est la dernière
    # qui porte la clé réellement choisie.
    cle1 = premier["ledger"][-1].get("key_id")
    cle2 = second["ledger"][-1].get("key_id")
    if len(second["ledger"]) != 2:
        return False, f"{len(second['ledger'])} lignes au lieu de deux : {second['ledger']}"
    if cle1 != "OPENROUTER_API_KEY":
        return False, f"première clé utilisée={cle1!r}"
    if cle2 != "OPENROUTER_API_KEY_2":
        return False, (
            f"deuxième clé utilisée={cle2!r} : la clé la moins sollicitée "
            "de la fenêtre doit passer en premier"
        )
    return True, "rotation vers la clé la moins utilisée, sans intervention"


def _cas_sortie_extraction_vide():
    """Texte non vide mais aucun code après extraction : échec, sans nouvel essai."""
    res = _run_broker(
        ["--raw-code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["```python\n\n```"],
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1)"
    if len(res["appels"]) != 1:
        return False, (
            f"{len(res['appels'])} appels : une extraction vide ne doit pas "
            "être retentée, la réponse du fournisseur était correcte"
        )
    if not res["recus"] or res["recus"][0].get("error_type") != "empty_response":
        return False, f"reçu={res['recus']}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 1 or erreurs[0].get("error_type") != "empty_response":
        return False, f"journal={res['ledger']}"
    return True, "sortie vide refusée, tracée en erreur, sans essai supplémentaire"


def _cas_reponse_vide_persiste_quota():
    """Réponse vide : le quota doit être incrémenté, jamais remboursé."""
    res = _run_broker(
        [],
        catalogue=_seul_gratuit(keys_env=["OPENROUTER_API_KEY"]),
        keys=_CLES_STD,
        plan=["   ", None],
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 2:
        return False, f"{len(res['appels'])} appels (attendu 2 : vide puis réponse)"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 1 or erreurs[0].get("error_type") != "empty_response":
        return False, f"journal={res['ledger']}"
    cle = (res["quota_state"].get("keys") or {}).get(
        quota._key_slot("or", "OPENROUTER_API_KEY")
    )
    if not isinstance(cle, dict) or int(cle.get("count") or 0) != 2:
        return False, (
            f"compteur de clé={cle} : les deux appels sont partis, donc les "
            "deux doivent être comptés"
        )
    return True, "réponse vide non remboursée : deux appels comptés, une erreur tracée"


def _cas_syntaxe_sortie_3():
    res = _run_broker(
        ["--raw-code", "--check-syntax"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["```python\ndef f(:\n```"],
    )
    if res["code"] != 3:
        return False, f"code={res['code']} (attendu 3, distinct d'un échec d'appel)"
    if not res["recus"] or res["recus"][0].get("error_type") != "syntax_error":
        return False, f"reçu={res['recus']}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 1 or erreurs[0].get("error_type") != "syntax_error":
        return False, f"journal={res['ledger']}"
    return True, "code 3 pour une syntaxe invalide, avec ligne d'erreur au journal"


# --- Enveloppe JSON des modes de code (phase 2) ---
# Le code sain et le code cassé sont des constantes : les relire au même endroit
# que les assertions évite qu'une faute de frappe dans un cas le fasse passer
# pour un succès de la réparation. Pas de fin de ligne finale : `split_answer`
# retire celle du modèle, et le fichier écrit en rajoute exactement une.

_CODE_SAIN = "def f():\n    return 1"
_CODE_CASSE = "def f(:\n    return 1"


def _enveloppe_json(code, explication="renvoie 1"):
    """Enveloppe telle qu'un modèle docile la rendrait, sur une seule ligne."""
    return json.dumps({"explication": explication, "code": code})


def _cas_enveloppe_extrait_le_code():
    """Mode `code` : l'enveloppe est lue, seul le champ `code` est écrit."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_enveloppe_json(_CODE_SAIN)],
        output_name="genere.py",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, f"{len(res['appels'])} appels (attendu 1 : le code compile)"
    with open(res["output_path"], "r", encoding="utf-8") as f:
        contenu = f.read()
    if contenu != _CODE_SAIN + "\n":
        return False, f"fichier={contenu!r} (ni JSON ni explication ne doivent y entrer)"
    if "[ai_broker] explication : renvoie 1" not in res["stderr"]:
        return False, f"explication absente de stderr : {res['stderr']!r}"
    if res["stdout"]:
        return False, f"stdout non vide alors que `-o` était fourni : {res['stdout']!r}"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "ok" or recu.get("repairs") not in (0, None):
        return False, f"reçu={recu}"
    if [l for l in res["ledger"] if l.get("status") == "error"]:
        return False, f"lignes d'erreur inattendues : {res['ledger']}"
    return True, "enveloppe lue, code seul écrit, explication sur stderr"


def _cas_enveloppe_repare_la_syntaxe():
    """Code invalide → l'erreur exacte repart au modèle, qui corrige."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_enveloppe_json(_CODE_CASSE), _enveloppe_json(_CODE_SAIN)],
        output_name="genere.py",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 2:
        return False, f"{len(res['appels'])} appels (attendu 2 : jet puis réparation)"
    relance = res["appels"][1]["prompt"]
    if "Erreur de compilation" not in relance or "SyntaxError" not in relance:
        return False, f"la relance ne porte pas l'erreur : {relance[:200]!r}"
    if "def f(:" not in relance:
        return False, (
            "la relance ne replace pas le code fautif devant le modèle : il "
            "corrigerait un programme qu'il ne voit plus"
        )
    with open(res["output_path"], "r", encoding="utf-8") as f:
        contenu = f.read()
    if contenu != _CODE_SAIN + "\n":
        return False, f"fichier={contenu!r}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    oks = [l for l in res["ledger"] if l.get("status") == "ok"]
    if len(erreurs) != 1 or erreurs[0].get("error_type") != "syntax_error":
        return False, f"journal d'erreur={erreurs}"
    if erreurs[0].get("repair_round") != 0:
        return False, (
            f"rang de réparation={erreurs[0].get('repair_round')} : l'échec du "
            "premier jet doit se lire au rang 0"
        )
    if len(oks) != 1:
        return False, f"lignes ok={oks} (une seule, celle du code retenu)"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "ok" or recu.get("repairs") != 1:
        return False, f"reçu={recu}"
    return True, "un tour de réparation suffit, l'échec intermédiaire reste tracé"


def _cas_enveloppe_reparation_epuisee():
    """Deux codes cassés de suite : échec explicite en 3, jamais une boucle."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        # Un seul élément : la relance reçoit le même code cassé. C'est le pire
        # cas — un modèle qui ne se corrige pas ne doit pas être relancé sans fin.
        plan=[_enveloppe_json(_CODE_CASSE)],
    )
    if res["code"] != 3:
        return False, f"code={res['code']} (attendu 3)"
    if len(res["appels"]) != 2:
        return False, (
            f"{len(res['appels'])} appels : un seul tour de réparation est "
            "autorisé par défaut, la relance ne doit pas s'enchaîner"
        )
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 2:
        return False, f"{len(erreurs)} lignes d'erreur (attendu 2 : jet et relance)"
    if any(e.get("error_type") != "syntax_error" for e in erreurs):
        return False, f"types d'erreur={[e.get('error_type') for e in erreurs]}"
    rangs = sorted(e.get("repair_round") for e in erreurs)
    if rangs != [0, 1]:
        return False, f"rangs de réparation={rangs} (attendu [0, 1])"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("error_type") != "syntax_error" or recu.get("repairs") != 1:
        return False, f"reçu={recu}"
    return True, "réparation tentée une fois, puis échec explicite en 3"


def _cas_syntaxe_cascade_vers_modele_suivant():
    """Code cassé sur le premier modèle → réparation échoue → cascade vers le
    deuxième modèle qui renvoie du code sain → succès final en 0."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        # appel 0 : premier jet modèle 1 → code cassé
        # appel 1 : réparation modèle 1 → encore cassé (max_reparations=1 épuisé)
        # appel 2 : premier jet modèle 2 → code sain
        plan=[
            _enveloppe_json(_CODE_CASSE),
            _enveloppe_json(_CODE_CASSE),
            _enveloppe_json(_CODE_SAIN),
        ],
        output_name="genere.py",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 3:
        return False, (
            f"{len(res['appels'])} appels : attendu 3 "
            "(2 sur modèle 1 cassé + 1 sur modèle 2 sain)"
        )
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 2:
        return False, f"{len(erreurs)} erreurs au journal (attendu 2 : jet + réparation)"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "ok":
        return False, f"reçu={recu}"
    return True, "syntaxe cassée sur modèle 1 → cascade → succès sur modèle 2"


def _cas_syntaxe_cascade_tous_epuises():
    """Tous les modèles génèrent du code cassé → code de retour 3, pas 1."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_enveloppe_json(_CODE_CASSE)],
    )
    if res["code"] != 3:
        return False, (
            f"code={res['code']} (attendu 3 : sortie inexploitable, "
            "distinct d'un échec de transport en 1)"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("error_type") != "syntax_error":
        return False, f"reçu={recu}"
    return True, "cascade épuisée sur syntaxe cassée → code 3 propagé"


def _cas_enveloppe_absente_repli():
    """Modèle qui ignore le format → bloc markdown classique, comportement d'hier."""
    res = _run_broker(
        ["--mode", "code"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["Voici le programme :\n```python\nx = 1\n```\n"],
        output_name="genere.py",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, f"{len(res['appels'])} appels (le repli ne relance rien)"
    with open(res["output_path"], "r", encoding="utf-8") as f:
        contenu = f.read()
    if contenu != "x = 1\n":
        return False, f"fichier={contenu!r}"
    if "[ai_broker] explication" in res["stderr"]:
        return False, "une explication est annoncée alors qu'il n'y en a pas"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("explanation_chars") not in (0, None):
        return False, f"reçu={recu}"
    if recu.get("repairs") not in (0, None):
        return False, f"reçu={recu}"
    return True, "un modèle qui ignore l'enveloppe reste utilisable (repli historique)"


# --- Mode `context` : cache disque et pavage (phase 2) ---
# Le quota se compte en **requêtes** : cinq documents lus un par un coûtent cinq
# appels, le même lot en un seul en coûte un. Le cache disque répond à l'exigence
# jumelle (« jamais relu deux fois ») en indexant le texte **tel qu'il part** — le
# texte numéroté — de sorte qu'une seule lettre changée invalide l'entrée sans
# durée de vie à deviner.

def _fichiers_contexte(contenus):
    """Écrit des documents jetables dans un dossier suivi par le banc.

    `contenus` est une suite de `(nom, texte)`. Rend les chemins dans l'ordre
    donné, qui est celui de la ligne de commande et donc celui de la sortie
    attendue.
    """
    dossier = tempfile.mkdtemp(prefix="banc-contexte-")
    _TEMP_DIRS.append(dossier)
    chemins = []
    for nom, texte in contenus:
        chemin = os.path.join(dossier, nom)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(texte)
        chemins.append(chemin)
    return chemins


def _reponse_contexte(paires):
    """Réponse de pavage : `paires` = `(chemin, titre)`, dans l'ordre voulu."""
    return json.dumps(
        {
            "documents": [
                {"path": chemin, "title": titre, "key_points": []}
                for chemin, titre in paires
            ]
        },
        ensure_ascii=False,
    )


def _usage_contexte(prefixe):
    """Bac à sable partagé par deux exécutions : c'est lui qui porte l'index."""
    usage = tempfile.mkdtemp(prefix=prefixe)
    _TEMP_DIRS.append(usage)
    return usage


def _cas_contexte_pave_et_cache():
    """Deux documents, un seul appel ; le second passage ne coûte rien."""
    premier, second = _fichiers_contexte(
        [("premier.md", "alpha\n"), ("second.md", "beta\n")]
    )
    usage = _usage_contexte("banc-contexte-pave-")
    argv = ["--mode", "context", "-f", premier, "-f", second]

    res = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_reponse_contexte([(premier, "Alpha"), (second, "Beta")])],
        usage_dir=usage,
        prompt="",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, (
            f"{len(res['appels'])} appels (attendu 1 : le quota se compte en "
            "requêtes, deux documents ne doivent pas en coûter deux)"
        )
    envoi = res["appels"][0]["prompt"]
    if "1\talpha" not in envoi or "1\tbeta" not in envoi:
        return False, (
            "les deux documents numérotés doivent tenir dans le même prompt — "
            f"c'est cette numérotation qui rend une ligne citable : {envoi[:300]!r}"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("documents") != 2 or recu.get("paved") not in (0, None):
        return False, f"reçu du premier passage={recu}"
    if len(res["ledger"]) != 1:
        return False, f"journal={res['ledger']}"

    res2 = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        # Plan vide : tout appel serait enregistré dans `appels`, donc la moindre
        # requête rendrait ce cas rouge.
        plan=[],
        usage_dir=usage,
        prompt="",
    )
    if res2["code"] != 0:
        return False, f"2e passage code={res2['code']} stderr={res2['stderr'].strip()!r}"
    if res2["appels"]:
        return False, (
            "le second passage a rappelé le modèle : l'index disque n'a pas été "
            f"relu ({len(res2['appels'])} appels)"
        )
    recu2 = res2["recus"][0] if res2["recus"] else {}
    if recu2.get("cached") is not True or recu2.get("calls") != 0:
        return False, f"reçu du 2e passage={recu2}"
    if recu2.get("documents") != 2:
        return False, f"reçu du 2e passage={recu2}"
    # Rien n'est parti : le reçu le dit avec les mêmes champs que les autres, et
    # sans inventer de fenêtre de fournisseur — aucun fournisseur n'a été
    # sollicité, il n'y a donc aucun quota à afficher.
    if recu2.get("cost_usd") != 0.0:
        return False, (
            f"coût d'un service rendu par le cache={recu2.get('cost_usd')!r} "
            "(attendu 0,0)"
        )
    ecart = _ecart_quota(recu2, {"paid_cap_usd": 0.0, "paid_spent_usd": 0.0})
    if ecart:
        return False, f"reçu du 2e passage : {ecart}"
    if "window" in (recu2.get("quota") or {}):
        return False, (
            f"quota du reçu servi par le cache={recu2.get('quota')!r} : aucun appel "
            "n'est parti, aucune fenêtre de fournisseur ne doit être déclarée"
        )
    if len(res2["ledger"]) != 1:
        return False, (
            f"journal={res2['ledger']} : un service rendu par le cache n'est pas "
            "un appel parti, il ne doit pas ajouter de ligne d'usage"
        )
    if res2["stdout"] != res["stdout"]:
        return False, (
            "la sortie servie par le cache doit être identique à celle du premier "
            f"passage : {res2['stdout']!r} != {res['stdout']!r}"
        )
    return True, "deux documents en un appel, puis zéro requête grâce à l'index"


def _cas_contexte_partiel():
    """Un document déjà indexé ne repart pas, l'ordre de sortie reste celui de la CLI."""
    premier, second, troisieme = _fichiers_contexte(
        [
            ("premier.md", "alpha\n"),
            ("second.md", "beta\n"),
            ("troisieme.md", "gamma\n"),
        ]
    )
    usage = _usage_contexte("banc-contexte-partiel-")

    amorce = _run_broker(
        ["--mode", "context", "-f", premier],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_reponse_contexte([(premier, "Alpha")])],
        usage_dir=usage,
        prompt="",
    )
    if amorce["code"] != 0 or len(amorce["appels"]) != 1:
        return False, (
            f"amorce : code={amorce['code']} appels={len(amorce['appels'])} "
            f"stderr={amorce['stderr'].strip()!r}"
        )

    # Réponse donnée **dans le désordre** : seul l'appariement par le chemin rendu
    # par le modèle remet les analyses dans l'ordre de la ligne de commande. Un
    # appariement par position produirait ici « Alpha, Gamma, Beta ».
    res = _run_broker(
        ["--mode", "context", "-f", premier, "-f", second, "-f", troisieme],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[_reponse_contexte([(troisieme, "Gamma"), (second, "Beta")])],
        usage_dir=usage,
        prompt="",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, (
            f"{len(res['appels'])} appels (attendu 1 : les documents déjà indexés "
            "ne repartent pas, les nouveaux partent ensemble)"
        )
    envoi = res["appels"][0]["prompt"]
    if "alpha" in envoi:
        return False, "le document déjà indexé est reparti au modèle"
    if "beta" not in envoi or "gamma" not in envoi:
        return False, "les deux documents nouveaux doivent partir ensemble"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("documents") != 3 or recu.get("paved") != 1:
        return False, f"reçu={recu}"
    titres = [d.get("title") for d in json.loads(res["stdout"]).get("documents", [])]
    if titres != ["Alpha", "Beta", "Gamma"]:
        return False, (
            f"ordre de sortie={titres} (attendu celui de la ligne de commande : un "
            "digest de session ne doit pas dépendre de l'ordre de la réponse)"
        )
    return True, "un seul document nouveau part, la réponse en désordre est rangée"


def _cas_contexte_json_invalide():
    """Réponse inexploitable : code 4, rien de rangé, le document repart au tour suivant."""
    (document,) = _fichiers_contexte([("doc.md", "alpha\n")])
    usage = _usage_contexte("banc-contexte-json-invalide-")
    argv = ["--mode", "context", "-f", document]

    res = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["bien sûr ! voici mon avis sur ce document, sans le moindre JSON"],
        usage_dir=usage,
        prompt="",
    )
    if res["code"] != 4:
        return False, (
            f"code={res['code']} (attendu 4 : distinct d'un échec de transport, pour "
            "qu'un appelant shell sache que le modèle a répondu à côté)"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "error" or recu.get("error_type") != "invalid_json":
        return False, f"reçu={recu}"
    # Deux lignes d'erreur : le premier jet fautif, puis la relance restée fautive.
    # `repair_round` est ce qui les distingue — sans lui, un tableau de bord ne
    # peut pas dire si le modèle a dérapé une fois ou deux.
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 2 or any(l.get("error_type") != "invalid_json" for l in erreurs):
        return False, f"journal={res['ledger']}"
    if [l.get("repair_round") for l in erreurs] != [0, 1]:
        return False, (
            "les deux lignes d'erreur doivent se distinguer par leur tour de "
            f"réparation (0 = premier jet, 1 = relance) : {erreurs}"
        )
    if [l for l in res["ledger"] if l.get("status") == "ok"]:
        return False, "une ligne d'usage ok alors que rien n'a été rangé"
    if res["stdout"]:
        return False, f"stdout={res['stdout']!r} : rien de valide ne doit sortir"
    if recu.get("repairs") != 1 or recu.get("documents") != 1 or recu.get("paved") != 0:
        return False, (
            "le reçu d'échec doit se relire avec la grille d'un succès (tours de "
            f"réparation, documents, pavés) : {recu}"
        )
    # La relance doit pouvoir refaire le travail : la demande d'origine est
    # recopiée, avec la réponse fautive. Sans l'une ou l'autre, la relance est un
    # appel de plus qui ne peut pas aboutir — le pire des deux mondes.
    relance = res["appels"][1]["prompt"] if len(res["appels"]) > 1 else ""
    if "1\talpha" not in relance or "sans le moindre JSON" not in relance:
        return False, (
            "la relance doit recopier le document numéroté **et** la réponse "
            f"fautive : {relance[:400]!r}"
        )

    res2 = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["toujours pas du JSON"],
        usage_dir=usage,
        prompt="",
    )
    if len(res2["appels"]) != 2:
        return False, (
            "un échec ne doit rien mettre en cache : au tour suivant, le document "
            "doit repartir — un premier jet puis une relance, donc "
            f"{len(res2['appels'])} appels (attendu 2)"
        )
    return True, "code 4, rien de rangé, le document repart au tour suivant"


def _cas_contexte_repare_le_json():
    """Un premier jet illisible, une relance conforme : le travail aboutit.

    C'est le cœur du mécanisme 3 du plan (§6) : la garantie n'est pas « le modèle
    obéit toujours », c'est « un dérapage de format se rattrape **une** fois, et le
    rattrapage est prouvé ». La preuve est ici le rangement au cache, qui n'a lieu
    qu'après validation — un objet à moitié valide n'atteint jamais le disque.
    """
    (document,) = _fichiers_contexte([("doc.md", "alpha\n")])
    usage = _usage_contexte("banc-contexte-repare-")
    argv = ["--mode", "context", "-f", document]
    res = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["pas du JSON du tout", _reponse_contexte([(document, "Alpha")])],
        usage_dir=usage,
        prompt="",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 2:
        return False, (
            f"{len(res['appels'])} appels (attendu 2 : le premier jet fautif, puis "
            "la relance qui répare)"
        )
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    oks = [l for l in res["ledger"] if l.get("status") == "ok"]
    if len(erreurs) != 1 or erreurs[0].get("repair_round") != 0 or len(oks) != 1:
        return False, (
            f"journal={res['ledger']} : une réparation réussie laisse exactement une "
            "ligne d'erreur (premier jet, tour 0) et une ligne ok (la relance)"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "ok" or recu.get("repairs") != 1:
        return False, f"reçu={recu}"
    titres = [d.get("title") for d in json.loads(res["stdout"]).get("documents", [])]
    if titres != ["Alpha"]:
        return False, f"sortie rangée={titres}"

    # Le rangement au cache est la preuve que la validation a précédé l'écriture :
    # au tour suivant, le document est servi par l'index, sans une seule requête.
    res2 = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[],
        usage_dir=usage,
        prompt="",
    )
    if res2["code"] != 0 or res2["appels"]:
        return False, (
            f"2e passage : code={res2['code']} appels={len(res2['appels'])} — un "
            "document validé après réparation doit être servi par l'index"
        )
    return True, "dérapage rattrapé en une relance, puis servi par le cache"


def _cas_contexte_cache_invalide():
    """`--no-cache` n'écrit rien ; une seule lettre changée périme l'entrée."""
    (document,) = _fichiers_contexte([("doc.md", "alpha\n")])
    usage = _usage_contexte("banc-contexte-cache-invalide-")
    argv = ["--mode", "context", "-f", document]
    reponse = _reponse_contexte([(document, "Alpha")])

    res = _run_broker(
        ["--no-cache"] + argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[reponse],
        usage_dir=usage,
        prompt="",
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, (
            f"amorce --no-cache : code={res['code']} appels={len(res['appels'])} "
            f"stderr={res['stderr'].strip()!r}"
        )
    chemin_index = os.path.join(usage, "context", context_cache.INDEX_NAME)
    if os.path.exists(chemin_index):
        with open(chemin_index, "r", encoding="utf-8") as f:
            index = json.load(f)
        if index.get("documents"):
            return False, (
                "`--no-cache` a quand même mémorisé l'analyse : l'option veut dire "
                "« ne me sers pas du cache », donc aussi « n'y écris rien »"
            )

    res2 = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[reponse],
        usage_dir=usage,
        prompt="",
    )
    if len(res2["appels"]) != 1:
        return False, (
            "après un passage `--no-cache`, l'index doit être vierge : le document "
            f"doit repartir ({len(res2['appels'])} appels)"
        )

    # Une seule lettre change : l'empreinte porte sur le texte **envoyé**, donc
    # l'entrée devient caduque sans la moindre durée de vie à deviner.
    with open(document, "w", encoding="utf-8") as f:
        f.write("alpha!\n")
    res3 = _run_broker(
        argv,
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[reponse],
        usage_dir=usage,
        prompt="",
    )
    if len(res3["appels"]) != 1:
        return False, "un document corrigé a été resservi depuis le cache"
    if "alpha!" not in res3["appels"][0]["prompt"]:
        return False, "le prompt ne porte pas la version corrigée du document"
    return True, "`--no-cache` n'écrit rien, une lettre changée périme l'entrée"


# --- Mode `search` : le disque d'abord (phase 2) ---
# Le quota se compte en **requêtes** : le cas nominal du mode ne doit en coûter
# aucune. Un appel n'a lieu que si deux fichiers distincts se disputent le
# premier rang, et le modèle ne choisit alors que dans une liste fermée qu'on lui
# a écrite — c'est ce qui rend son choix vérifiable au lieu de plausible.

def _arbre_recherche(fichiers):
    """Écrit un arbre jetable de `(chemin relatif, texte)` ; rend la racine.

    Les sous-dossiers sont créés au besoin : c'est leur existence qui permet de
    fabriquer deux fichiers de même nom, donc l'égalité de score qui déclenche —
    et elle seule — le départage par le modèle.
    """
    racine = tempfile.mkdtemp(prefix="banc-recherche-")
    _TEMP_DIRS.append(racine)
    for relatif, texte in fichiers:
        chemin = os.path.join(racine, relatif)
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(texte)
    return racine


def _cas_recherche_locale_sans_appel():
    """Un seul fichier tient le premier rang : la réponse vient du disque, sans requête."""
    racine = _arbre_recherche(
        [
            ("scripts/ai/quota.py", "def load_state():\n    return {}\n"),
            ("docs/notes.md", "la mémoire de quota vit dans quota.py\n"),
        ]
    )
    usage = _usage_contexte("banc-recherche-locale-")
    res = _run_broker(
        ["--mode", "search", "--root", racine],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        # Plan vide : la moindre requête atterrirait dans `appels` et rendrait ce
        # cas rouge — c'est précisément ce qu'il existe pour prouver.
        plan=[],
        usage_dir=usage,
        prompt="quota",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if res["appels"]:
        return False, (
            "le mode search a sollicité un modèle alors qu'un seul fichier tenait "
            f"le premier rang ({len(res['appels'])} appel(s)) : le disque suffisait"
        )
    charge = json.loads(res["stdout"])
    attendu = os.path.join(racine, "scripts", "ai", "quota.py")
    if charge.get("path") != attendu:
        return False, f"chemin rendu={charge.get('path')!r} (attendu {attendu!r})"
    if charge.get("disambiguated") is not False:
        return False, f"une réponse locale ne se déclare pas désambiguïsée : {charge}"
    if charge.get("scanned") != 2:
        return False, f"deux fichiers écrits, {charge.get('scanned')} parcouru(s)"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("local") is not True or recu.get("calls") != 0:
        return False, f"reçu={recu}"
    if recu.get("cost_usd") != 0.0:
        return False, f"coût={recu.get('cost_usd')!r} : lire le disque ne coûte rien"
    ecart = _ecart_quota(recu, {"paid_cap_usd": 0.0, "paid_spent_usd": 0.0})
    if ecart:
        return False, f"reçu local : {ecart}"
    if "window" in (recu.get("quota") or {}):
        return False, (
            f"quota du reçu local={recu.get('quota')!r} : une recherche locale "
            "n'ouvre aucun quota de fournisseur, elle n'en déclare donc aucun"
        )
    if res["ledger"]:
        return False, (
            f"journal={res['ledger']} : une réponse purement locale n'est pas un "
            "appel parti, elle ne doit pas consommer de quota — le journal compte "
            "les requêtes, c'est la seule mesure qui dit ce que coûte la délégation"
        )
    return True, "un fichier sans concurrent : réponse locale, zéro appel, zéro quota"


def _cas_recherche_desambiguisee():
    """Deux fichiers à égalité : un seul appel, choix contraint à la liste locale."""
    racine = _arbre_recherche(
        [
            ("a/rapport.md", "rapport annuel\n"),
            ("b/rapport.md", "rapport trimestriel\n"),
        ]
    )
    usage = _usage_contexte("banc-recherche-desambigu-")
    premier = os.path.join(racine, "a", "rapport.md")
    second = os.path.join(racine, "b", "rapport.md")
    res = _run_broker(
        ["--mode", "search", "--root", racine],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[
            json.dumps(
                {"path": second, "reason": "le trimestriel est le plus récent"},
                ensure_ascii=False,
            )
        ],
        usage_dir=usage,
        prompt="rapport",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, (
            f"{len(res['appels'])} appel(s) (attendu 1 : une égalité de score se "
            "règle en un seul départage, pas un par candidat)"
        )
    envoi = res["appels"][0]["prompt"]
    if premier not in envoi or second not in envoi:
        return False, (
            "le prompt de départage doit porter les deux candidats : le modèle "
            "choisit dans une liste fermée, il ne parcourt pas le dépôt "
            f"({envoi[:300]!r})"
        )
    charge = json.loads(res["stdout"])
    if charge.get("path") != second:
        return False, (
            f"chemin rendu={charge.get('path')!r} (attendu {second!r}) : le chemin "
            "sort **tel qu'il figurait dans la liste**, pas dans la graphie du modèle"
        )
    if charge.get("disambiguated") is not True or not charge.get("reason"):
        return False, f"sortie={charge}"
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("disambiguated") is not True or recu.get("path") != second:
        return False, f"reçu={recu}"
    if len(res["ledger"]) != 1:
        return False, (
            f"journal={res['ledger']} : le départage est un appel réel, il compte "
            "une fois et une seule"
        )
    return True, "égalité de score → un appel, choix restreint à la liste locale"


def _cas_recherche_choix_invalide():
    """Un chemin hors de la liste fermée : code 4, tracé, rien de rendu."""
    racine = _arbre_recherche(
        [
            ("a/rapport.md", "rapport annuel\n"),
            ("b/rapport.md", "rapport trimestriel\n"),
        ]
    )
    usage = _usage_contexte("banc-recherche-invalide-")
    res = _run_broker(
        ["--mode", "search", "--root", racine],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[json.dumps({"path": "/tmp/invente.md", "reason": "je crois"}, ensure_ascii=False)],
        usage_dir=usage,
        prompt="rapport",
    )
    if res["code"] != 4:
        return False, (
            f"code={res['code']} (attendu 4 : un chemin inventé doit échouer "
            "explicitement, jamais être rendu à un appelant qui le rouvrirait "
            f"les yeux fermés) stderr={res['stderr'].strip()!r}"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("status") != "error" or recu.get("error_type") != "invalid_json":
        return False, f"reçu={recu}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 2 or [l.get("repair_round") for l in erreurs] != [0, 1]:
        return False, (
            f"journal={res['ledger']} : l'appel est bien parti, il doit laisser une "
            "ligne d'erreur pour le premier jet **et** une pour la relance — et "
            "surtout pas une ligne « ok »"
        )
    if [l for l in res["ledger"] if l.get("status") == "ok"]:
        return False, f"journal={res['ledger']} : rien n'a été rendu, donc pas de ligne ok"
    # La relance doit rappeler la liste fermée : un modèle à qui on ne redonne pas
    # les candidats n'a plus aucun moyen de choisir un chemin réel, et la seule
    # chance de réparation serait un appel gratuit perdu de plus.
    premier = os.path.join(racine, "a", "rapport.md")
    second = os.path.join(racine, "b", "rapport.md")
    relance = res["appels"][1]["prompt"] if len(res["appels"]) > 1 else ""
    if premier not in relance or second not in relance:
        return False, (
            "la relance doit recopier la liste fermée des candidats, sans quoi le "
            f"modèle ne peut que réinventer un chemin : {relance[:400]!r}"
        )
    return True, "chemin hors liste → code 4, deux lignes d'erreur, aucune sortie utile"


def _cas_recherche_repare_le_json():
    """Chemin hors liste, puis choix conforme : le départage se rattrape.

    La liste fermée est rappelée à la relance, donc le modèle peut à nouveau
    choisir un chemin réel — et le chemin rendu est celui de la liste, jamais la
    graphie du modèle.
    """
    racine = _arbre_recherche(
        [
            ("a/rapport.md", "rapport annuel\n"),
            ("b/rapport.md", "rapport trimestriel\n"),
        ]
    )
    usage = _usage_contexte("banc-recherche-repare-")
    second = os.path.join(racine, "b", "rapport.md")
    res = _run_broker(
        ["--mode", "search", "--root", racine],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[
            json.dumps({"path": "/tmp/invente.md", "reason": "je crois"}, ensure_ascii=False),
            json.dumps({"path": second, "reason": "le trimestriel"}, ensure_ascii=False),
        ],
        usage_dir=usage,
        prompt="rapport",
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 2:
        return False, (
            f"{len(res['appels'])} appels (attendu 2 : chemin inventé au premier jet, "
            "puis choix conforme après relance)"
        )
    charge = json.loads(res["stdout"])
    if charge.get("path") != second or charge.get("disambiguated") is not True:
        return False, f"sortie={charge}"
    if not charge.get("reason"):
        return False, f"le motif du choix doit être rendu : {charge}"
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 1 or erreurs[0].get("repair_round") != 0:
        return False, f"journal={res['ledger']}"
    return True, "chemin hors liste corrigé en une relance, choix rendu tel quel"


def _cas_recherche_aucun_resultat():
    """Rien ne correspond : échec en 1, tracé, et pas un seul appel."""
    racine = _arbre_recherche([("docs/notes.md", "rien de pertinent ici\n")])
    usage = _usage_contexte("banc-recherche-aucun-")
    res = _run_broker(
        ["--mode", "search", "--root", racine],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[],
        usage_dir=usage,
        prompt="zorglub-introuvable",
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1) stderr={res['stderr'].strip()!r}"
    if res["appels"]:
        return False, (
            "une recherche locale sans correspondance ne doit déclencher aucun "
            f"appel ({len(res['appels'])}) : le modèle n'a rien à départager"
        )
    recu = res["recus"][0] if res["recus"] else {}
    if recu.get("error_type") != "no_match":
        return False, f"reçu={recu}"
    if res["ledger"]:
        return False, f"journal={res['ledger']} (aucun appel, donc aucune ligne)"
    return True, "aucune correspondance locale → échec en 1, tracé, zéro appel"


def _cas_quarantaine_403():
    """403 : la clé est écartée, tout le fournisseur tombe, on continue ailleurs."""
    res = _run_broker(
        [], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[_http(403), None]
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if [a["provider"] for a in res["appels"]] != ["or", "gm"]:
        return False, (
            "fournisseurs appelés="
            f"{[a['provider'] for a in res['appels']]} (attendu or puis gm : "
            "or-free-2 ne doit pas être tenté après un 403 de clé)"
        )
    entree = (res["quota_state"].get("keys") or {}).get(
        quota._key_slot("or", "OPENROUTER_API_KEY")
    )
    if not isinstance(entree, dict) or not entree.get("bad_until"):
        return False, f"clé non mise en quarantaine : {entree}"
    if float(entree["bad_until"]) <= time.time():
        return False, "quarantaine déjà expirée"
    if not res["ledger"] or res["ledger"][0].get("error_type") != "http_403":
        return False, f"journal={res['ledger']}"
    return True, "clé mise en quarantaine, fournisseur écarté, Gemini prend la main"


def _cas_modele_retire_404():
    res = _run_broker(
        [], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[_http(404), None]
    )
    if res["code"] != 0 or len(res["appels"]) != 2:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if res["appels"][1]["model"] != "or-free-2":
        return False, f"repli sur {res['appels'][1]['model']}"
    entree = (res["health_state"].get("entries") or {}).get(
        health.key("or", "or-free-1")
    )
    if not isinstance(entree, dict) or not entree.get("retired"):
        return False, f"modèle non marqué retiré : {entree}"
    return True, "un 404 retire le modèle jusqu'au prochain rafraîchissement"


def _cas_modele_en_cooldown():
    etat = _etat_sante(
        "or",
        "or-free-1",
        failures=1,
        last_status=429,
        cooldown_until=time.time() + 600,
    )
    res = _run_broker(
        ["--list-candidates"],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        health_state=etat,
    )
    if res["code"] != 0:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if "cooldown" not in res["stdout"]:
        return False, "la raison du cooldown n'est pas exposée par `--list-candidates`"
    # `explain()` rend les entrées dans l'ordre du catalogue : le modèle écarté
    # apparaît donc avant le suivant. Ce qui compte est le premier **inclus**.
    incluses = [l for l in res["stdout"].splitlines() if l.startswith("[inclus]")]
    if not incluses or "or:or-free-2" not in incluses[0]:
        return False, f"premier inclus={incluses[0] if incluses else 'aucun'!r}"
    suite = _run_broker([], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[None],
                        health_state=etat)
    if not suite["appels"] or suite["appels"][0]["model"] != "or-free-2":
        return False, "le modèle en cooldown a quand même été appelé"
    return True, "modèle en cooldown écarté, motif publié, suivant appelé"


def _cas_cle_rejetee():
    etat = _etat_cle(
        "or",
        "OPENROUTER_API_KEY",
        window="daily",
        count=0,
        bad_until=time.time() + 600,
    )
    res = _run_broker(
        [],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        quota_state=etat,
        plan=[None],
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1)"
    if res["appels"]:
        return False, "un appel est parti avec une clé en quarantaine"
    if "rejetée (401/403)" not in res["stderr"]:
        return False, f"motif du refus absent : {res['stderr']!r}"
    return True, "clé en quarantaine : refus explicite, aucun appel émis"


def _cas_quota_fenetre_atteinte():
    etat = _etat_cle("or", "OPENROUTER_API_KEY", window="daily", count=1)
    res = _run_broker(
        [],
        catalogue=_seul_gratuit(limit=1),
        keys=_CLES_STD,
        quota_state=etat,
        plan=[None],
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1)"
    if res["appels"]:
        return False, "un appel est parti alors que la fenêtre est pleine"
    if "quota daily atteint (1)" not in res["stderr"]:
        return False, f"motif du refus absent : {res['stderr']!r}"
    return True, "quota de fenêtre atteint : refus explicite, aucun appel émis"


def _cas_fenetre_changee():
    """Un compteur d'hier ne doit pas bloquer l'appel d'aujourd'hui."""
    etat = _etat_cle("or", "OPENROUTER_API_KEY", window="daily", count=5)
    etat["keys"][quota._key_slot("or", "OPENROUTER_API_KEY")]["window"] = "1970-01-01"
    res = _run_broker(
        [],
        catalogue=_seul_gratuit(limit=1),
        keys=_CLES_STD,
        quota_state=etat,
        plan=[None],
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    cle = (res["quota_state"].get("keys") or {}).get(
        quota._key_slot("or", "OPENROUTER_API_KEY")
    )
    if not isinstance(cle, dict) or int(cle.get("count") or 0) != 1:
        return False, f"compteur non reparti de zéro : {cle}"
    return True, "compteur remis à zéro au changement de fenêtre"


def _cas_payant_refuse_sans_autorisation():
    res = _run_broker(
        [],
        catalogue=_paid_catalogue(),
        keys=_CLES_PAYANTES,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1)"
    if res["appels"]:
        return False, "un appel payant est parti sans autorisation"
    if "hors payants autorisés" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "payant refusé et motif publié quand rien ne l'autorise"


def _cas_payant_non_valide():
    res = _run_broker(
        ["--allow-paid"],
        catalogue=_paid_catalogue(paid_ok=False),
        keys=_CLES_PAYANTES,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 1 or res["appels"]:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if "payant non validé" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "un modèle payant non validé n'est jamais appelé, même autorisé"


def _cas_payant_prix_inconnu():
    """Un prix illisible doit refuser l'appel, jamais valoir « gratuit »."""
    cat = _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                models=[
                    {
                        "id": "or-prix-casse",
                        "rank": 10,
                        "free": False,
                        "paid_ok": True,
                        "pricing": {"prompt_per_1m": "n/a", "completion_per_1m": "n/a"},
                    }
                ],
            )
        }
    )
    res = _run_broker(
        ["--allow-paid"],
        catalogue=cat,
        keys=_CLES_STD,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 1 or res["appels"]:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if "prix inconnu" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "prix illisible traité comme un refus, jamais comme du gratuit"


def _cas_payant_prix_absent():
    """Un prix **absent** ne vaut jamais gratuit — les trois formes, d'un coup.

    Le cas précédent couvre un prix *illisible* (`"n/a"`, qui lève à la
    conversion). Celui-ci couvre l'**absence** de prix, que `is_free()` lisait
    comme deux zéros — donc comme une gratuité — et le payant partait sans
    `paid_allowlist` et hors plafond. Trois formes, dont celle que le
    rafraîchissement produisait avant la sentinelle :

    1. clé `pricing` absente ;
    2. deux zéros écrits sur un payant déclaré (`free: false`) ;
    3. champs de prix explicitement nuls.

    Si une seule de ces formes repasse pour du gratuit, un appel part et le cas
    échoue : c'est le seul juge qui compte.
    """
    cat = _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                models=[
                    {"id": "or-prix-manquant", "rank": 10, "free": False, "paid_ok": True},
                    {
                        "id": "or-zeros-fantomes",
                        "rank": 11,
                        "free": False,
                        "paid_ok": True,
                        "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    },
                    {
                        "id": "or-prix-nul-declare",
                        "rank": 12,
                        "free": False,
                        "paid_ok": True,
                        "pricing": {"prompt_per_1m": None, "completion_per_1m": None},
                    },
                ],
            )
        }
    )
    res = _run_broker(
        ["--allow-paid"],
        catalogue=cat,
        keys=_CLES_STD,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 1 or res["appels"]:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if "prix inconnu" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "prix absent (clé manquante, zéros fantômes ou champs nuls) refusé"


def _cas_plafond_zero():
    res = _run_broker(
        ["--allow-paid"],
        catalogue=_paid_catalogue(),
        keys=_CLES_PAYANTES,
        plan=[None],
    )
    if res["code"] != 1 or res["appels"]:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if "plafond journalier à 0" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "sans plafond déclaré, aucun euro ne peut partir"


def _cas_plafond_depasse():
    """Coût estimé ≈ 0,00513 $ pour 1 jeton d'entrée, plafond fixé à 0,0001 $."""
    res = _run_broker(
        ["--allow-paid"],
        catalogue=_paid_catalogue(),
        keys=_CLES_PAYANTES,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "0.0001"},
        plan=[None],
    )
    if res["code"] != 1 or res["appels"]:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    if "plafond journalier atteint" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    return True, "coût estimé supérieur au plafond : refus avant envoi"


def _cas_escalade_autorise_payant():
    res = _run_broker(
        ["--escalate"],
        catalogue=_paid_catalogue(),
        keys=_CLES_PAYANTES,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, f"code={res['code']}, appels={len(res['appels'])}"
    ligne = res["ledger"][0] if res["ledger"] else {}
    if ligne.get("paid") is not True:
        return False, f"ligne de journal non payante : {ligne}"
    if not float(ligne.get("cost_usd") or 0.0) > 0.0:
        return False, f"coût non imputé : {ligne.get('cost_usd')}"
    depense = float((res["quota_state"].get("paid") or {}).get("spent_usd") or 0.0)
    if depense <= 0.0:
        return False, "compteur payant du jour non incrémenté"
    if not res["recus"] or res["recus"][0].get("paid") is not True:
        return False, f"reçu non marqué payant : {res['recus']}"
    return True, f"escalade payante autorisée, {depense:.6f} $ imputés"


def _cas_mode_raisonnement_payant():
    """`reasoning` est déclaré payant d'emblée : pas besoin de drapeau."""
    res = _run_broker(
        ["--mode", "reasoning"],
        catalogue=_paid_catalogue(),
        keys=_CLES_PAYANTES,
        env={"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
        plan=[None],
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    ligne = res["ledger"][0] if res["ledger"] else {}
    if ligne.get("paid") is not True or ligne.get("mode") != "reasoning":
        return False, f"ligne de journal={ligne}"
    return True, "un mode déclaré payant ouvre le payant sans drapeau"


def _cas_provider_filtre_avant_troncature():
    """`--provider X` filtre **avant** le plafond, et reste strict.

    Défaut constaté en production le 26/09 sur le chemin payant de l'ouvrier
    (`ai_worker._decider_paye` → `--provider deepseek --mode reasoning`) : 23
    modèles gratuits remplissaient les `max_candidates` places, la troncature
    s'appliquait d'abord, et le filtre fournisseur — appliqué ensuite — ne
    trouvait plus rien. Le courtier répondait « aucun candidat utilisable » alors
    que la table de diagnostic montrait l'entrée DeepSeek « payant autorisé ».

    Trois temps, sur **le même catalogue** (plafond à 3, huit gratuits mieux
    classés que le payant) :

    1. le chemin sans filtre sert un gratuit et n'atteint jamais le payant —
       c'est la pré-condition du défaut ;
    2. `--provider ds` atteint le payant malgré la troncature ;
    3. `--provider gm`, absent du catalogue, refuse au lieu de retomber
       silencieusement sur un autre fournisseur.
    """
    cat = _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                models=[_model(f"or-free-{i}", rank=10 + i) for i in range(8)],
            ),
            "ds": _provider(
                "ds",
                base_url="https://api.deepseek.test/v1",
                keys_env=["DEEPSEEK_API_KEY"],
                models=[
                    _model(
                        "ds-paid",
                        free=False,
                        rank=900,
                        prompt_per_1m=10.0,
                        completion_per_1m=10.0,
                        paid_ok=True,
                    )
                ],
            ),
        },
        policy={"max_candidates": 3},
    )
    options = {
        "keys": {**_CLES_STD, **_CLES_PAYANTES},
        "env": {"RADAR_AI_DAILY_PAID_CAP_USD": "5.0"},
    }

    # 1. Contrôle : sans filtre, la troncature à 3 écarte le payant.
    defaut = _run_broker(
        ["--mode", "reasoning", "--allow-paid"], catalogue=cat, plan=[None], **options
    )
    if defaut["code"] != 0 or len(defaut["appels"]) != 1:
        return False, (
            f"contrôle : code={defaut['code']} appels={len(defaut['appels'])} "
            f"stderr={defaut['stderr'].strip()!r}"
        )
    if defaut["appels"][0]["provider"] != "or":
        return False, f"contrôle : le payant a été servi sans filtre ({defaut['appels'][0]})"

    # 2. Le filtre rattrape ce que la troncature avait écarté.
    res = _run_broker(
        ["--mode", "reasoning", "--provider", "ds", "--allow-paid"],
        catalogue=cat,
        plan=[None],
        **options,
    )
    if res["code"] != 0 or len(res["appels"]) != 1:
        return False, (
            f"code={res['code']} appels={len(res['appels'])} "
            f"stderr={res['stderr'].strip()!r}"
        )
    servi = res["appels"][0]
    if servi["provider"] != "ds" or servi["model"] != "ds-paid":
        return False, f"candidat servi = {servi['provider']}:{servi['model']}"

    # 3. Le filtre reste un filtre : un fournisseur absent ne retombe sur rien.
    vide = _run_broker(
        ["--mode", "reasoning", "--provider", "gm"],
        catalogue=cat,
        plan=[None],
        **options,
    )
    if vide["code"] != 1 or vide["appels"]:
        return False, f"fournisseur absent : code={vide['code']} appels={len(vide['appels'])}"
    if "gm" not in vide["stderr"]:
        return False, f"le refus ne nomme pas le fournisseur demandé : {vide['stderr']!r}"
    return True, "filtre fournisseur appliqué avant la troncature, et toujours strict"


def _cas_masquage_secret():
    """Le prompt doit être nettoyé **avant** la sérialisation du corps HTTP."""
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    res = _run_broker(
        [],
        catalogue=_std_catalogue(),
        keys=_CLES_STD,
        prompt=f"Explique ce fichier, la clé est {secret}",
    )
    if res["code"] != 0 or not res["appels"]:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    contenu = _message_utilisateur(res["appels"][0]["payload"])
    if secret in contenu:
        return False, "le secret est présent dans le corps de la requête"
    if "[masqué]" not in contenu:
        return False, f"aucun masquage visible : {contenu!r}"
    if not res["recus"] or int(res["recus"][0].get("redactions") or 0) < 1:
        return False, f"le reçu ne compte pas le masquage : {res['recus']}"
    return True, "secret masqué dans le corps envoyé et compté dans le reçu"


def _cas_json_mode_transmis():
    """`--json-output` part au fournisseur, et le JSON revient entier.

    Le drapeau transmis ne suffit pas à prouver la garantie : `json_object` ne dit
    pas *quel* JSON, la validation locale reste donc le juge. Ici la réponse est
    déjà conforme, donc aucune relance ne doit avoir lieu — un appel, pas deux.
    """
    charge = json.dumps({"ok": True, "points": ["a", "b"]})
    avec = _run_broker(
        ["--json-output"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=[charge],
    )
    sans = _run_broker([], catalogue=_seul_gratuit(), keys=_CLES_STD, plan=[charge])
    if not avec["appels"] or not sans["appels"]:
        return False, "aucun appel capturé"
    if avec["appels"][0].get("json_mode") is not True:
        return False, f"`--json-output` non transmis : {avec['appels'][0].get('json_mode')}"
    if sans["appels"][0].get("json_mode") is not False:
        return False, f"JSON activé sans le demander : {sans['appels'][0].get('json_mode')}"
    if avec["code"] != 0:
        return False, f"code={avec['code']} stderr={avec['stderr'].strip()!r}"
    if len(avec["appels"]) != 1:
        return False, (
            "une réponse déjà valide ne doit pas déclencher de relance, même sous le "
            f"contrat le plus faible (`json_object`) : {len(avec['appels'])} appels"
        )
    try:
        if json.loads(avec["stdout"]) != json.loads(charge):
            return False, f"JSON altéré en chemin : {avec['stdout']!r}"
    except ValueError as exc:
        return False, f"stdout illisible : {exc!r} — {avec['stdout']!r}"
    return True, "`--json-output` transmis, JSON rendu intact, absent sinon"


def _cas_response_format_capacites():
    """Un champ non déclaré par le catalogue ne doit pas partir : 400 garanti."""
    spec = providers.ProviderSpec(name="or", kind="openai", base_url="https://x.test/v1")
    adaptateur = providers.OpenAICompatProvider(spec)
    avec = adaptateur.build_payload(
        "m", "p", json_mode=True, supported_parameters=["response_format"]
    )
    sans = adaptateur.build_payload(
        "m", "p", json_mode=True, supported_parameters=["tools", "tool_choice"]
    )
    inconnu = adaptateur.build_payload("m", "p", json_mode=True, supported_parameters=None)
    if "response_format" not in avec:
        return False, "`response_format` omis alors que le modèle le déclare"
    if "response_format" in sans:
        return False, "`response_format` envoyé à un modèle qui ne le déclare pas"
    if "response_format" not in inconnu:
        return False, "capacités non publiées : le champ doit être conservé"
    return True, "capacités respectées : déclaré ⇒ envoyé, non déclaré ⇒ omis"


def _cas_json_schema_natif_envoye():
    """Capacité déclarée : le contrat strict part en `json_schema`, la relance le reprend.

    Prend le chemin réseau réel (adaptateur puis `urlopen` capturé) parce que c'est
    le seul qui prouve la traduction du schéma canonique en format du fournisseur.
    Le catalogue est assemblé sur place : `_seul_gratuit()` ne sait pas déclarer de
    capacités, et sans `structured_outputs` le contrat retomberait en `json_object`.
    """
    (document,) = _fichiers_contexte([("doc.md", "alpha\n")])
    catalogue = _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY"],
                quota_spec={"window": "daily", "limit": None},
                models=[_model("or-free-1", supports=["structured_outputs"])],
            )
        }
    )
    res = _run_broker(
        ["--mode", "context", "-f", document], catalogue=catalogue, keys=_CLES_STD
    )
    if res["code"] != 4:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 2:
        return False, (
            "un premier jet fautif puis une relance, donc 2 appels : "
            f"{len(res['appels'])}"
        )
    attendu = schema.pour_openai(schema.CONTEXTE_NOM, schema.CONTEXTE)
    for index, appel in enumerate(res["appels"]):
        envoye = appel["payload"].get("response_format")
        if envoye != attendu:
            return False, f"appel {index + 1} : `response_format`={envoye!r}"
    bloc = res["appels"][0]["payload"]["response_format"]["json_schema"]
    if bloc.get("strict") is not True:
        return False, (
            "un contrat non strict laisse le fournisseur générer hors schéma : "
            f"{bloc!r}"
        )
    relance = _message_utilisateur(res["appels"][1]["payload"])
    if "1\talpha" not in relance or "réponse ok" not in relance:
        return False, (
            "la relance doit porter le document numéroté et la réponse fautive : "
            f"{relance[:400]!r}"
        )
    return True, "contrat natif strict envoyé, repris tel quel pour la relance"


def _cas_json_schema_strict_ou_absent():
    """Trois paliers de garantie : `json_schema` déclaré, `json_object`, ou rien.

    Un `json_schema` supposé coûte un 400 sur le premier candidat gratuit : c'est
    le seul palier qui exige une **déclaration** du catalogue, pas une tolérance.
    """
    spec = providers.ProviderSpec(name="or", kind="openai", base_url="https://x.test/v1")
    adaptateur = providers.OpenAICompatProvider(spec)

    natif = adaptateur.build_payload(
        "m",
        "p",
        json_mode=True,
        json_schema=schema.CONTEXTE,
        schema_name=schema.CONTEXTE_NOM,
        supported_parameters=["structured_outputs"],
    )
    format_natif = natif.get("response_format") or {}
    if format_natif.get("type") != "json_schema":
        return False, f"format natif attendu : {format_natif!r}"
    bloc = format_natif.get("json_schema") or {}
    if bloc.get("name") != schema.CONTEXTE_NOM or bloc.get("strict") is not True:
        return False, f"nom ou caractère strict manquant : {bloc!r}"
    if "documents" not in json.dumps(bloc.get("schema") or {}):
        return False, f"le schéma canonique n'a pas suivi : {bloc.get('schema')!r}"

    drapeau = adaptateur.build_payload(
        "m", "p", json_mode=True, supported_parameters=["response_format"]
    )
    if drapeau.get("response_format") != {"type": "json_object"}:
        return False, f"repli `json_object` attendu : {drapeau.get('response_format')!r}"

    muet = adaptateur.build_payload("m", "p", supported_parameters=None)
    if "response_format" in muet:
        return False, f"aucun format demandé, aucun format envoyé : {sorted(muet)}"
    return True, "`json_schema` strict si déclaré, `json_object` sinon, rien sans demande"


def _cas_json_reparation_refusee():
    """`--max-repairs 0` : le contrat JSON échoue au premier dérapage, sans relance.

    La relance coûte un appel : un appelant qui budgète ses requêtes doit pouvoir
    l'interdire, et le refus doit être explicite (code 4) — jamais un silence.
    """
    (document,) = _fichiers_contexte([("doc.md", "alpha\n")])
    res = _run_broker(
        ["--mode", "context", "-f", document, "--max-repairs", "0"],
        catalogue=_seul_gratuit(),
        keys=_CLES_STD,
        plan=["pas du JSON"],
        prompt="",
    )
    if res["code"] != 4:
        return False, f"code={res['code']} stderr={res['stderr'].strip()!r}"
    if len(res["appels"]) != 1:
        return False, (
            "`--max-repairs 0` interdit la relance — c'est l'appel supplémentaire "
            f"qu'on refuse ({len(res['appels'])} appels)"
        )
    erreurs = [l for l in res["ledger"] if l.get("status") == "error"]
    if len(erreurs) != 1 or erreurs[0].get("repair_round") != 0:
        return False, f"journal={res['ledger']}"
    return True, "premier dérapage suffisant, relance refusée, échec explicite"


def _cas_effort_traduit_par_fournisseur():
    """`--effort` prend la graphie du fournisseur, et rien d'autre."""
    xai = providers.OpenAICompatProvider(
        providers.ProviderSpec(
            name="xai", kind="openai", base_url="https://x.test/v1", reasoning_style="effort"
        )
    )
    ouvert = providers.OpenAICompatProvider(
        providers.ProviderSpec(
            name="or",
            kind="openai",
            base_url="https://x.test/v1",
            reasoning_style="reasoning_effort",
        )
    )

    sur = xai.build_payload(
        "m", "p", effort="high", supported_parameters=["reasoning", "effort"]
    )
    if sur.get("reasoning") != {"effort": "high"}:
        return False, f"graphie `effort` absente : {sur.get('reasoning')!r}"
    if "reasoning_effort" in sur:
        return False, "les deux graphies sont parties : 400 garanti"

    ro = ouvert.build_payload("m", "p", effort="high", supported_parameters=["reasoning_effort"])
    if ro.get("reasoning_effort") != "high":
        return False, f"graphie `reasoning_effort` absente : {ro.get('reasoning_effort')!r}"
    if "reasoning" in ro:
        return False, "les deux graphies sont parties : 400 garanti"

    # Le modèle publie ses capacités et n'y déclare pas le raisonnement.
    overte = ouvert.build_payload("m", "p", effort="high", supported_parameters=["tools"])
    if "reasoning" in overte or "reasoning_effort" in overte:
        return False, f"champ non déclaré envoyé : {sorted(overte)}"

    # Le modèle ne déclare que l'autre graphie : on la prend, on ne devine pas.
    autre = xai.build_payload("m", "p", effort="high", supported_parameters=["reasoning_effort"])
    if autre.get("reasoning_effort") != "high" or "reasoning" in autre:
        return False, f"repli de graphie refusé : {autre}"

    # Capacités non publiées : la graphie déclarée du fournisseur est conservée.
    inconnu = xai.build_payload("m", "p", effort="low", supported_parameters=None)
    if inconnu.get("reasoning") != {"effort": "low"}:
        return False, f"capacités non publiées : {inconnu.get('reasoning')!r}"

    # Aucun style déclaré, aucune capacité publiée : on n'invente pas un champ.
    nu = providers.OpenAICompatProvider(
        providers.ProviderSpec(name="nu", kind="openai", base_url="https://x.test/v1")
    ).build_payload("m", "p", effort="high", supported_parameters=None)
    if "reasoning" in nu or "reasoning_effort" in nu:
        return False, f"graphie devinée sans déclaration : {sorted(nu)}"

    sans = xai.build_payload("m", "p", supported_parameters=["reasoning", "effort"])
    if "reasoning" in sans:
        return False, "champ de raisonnement envoyé sans `--effort`"
    return True, "graphie du fournisseur respectée, filtrée par les capacités, sinon rien"


def _cas_effort_gemini_thinking_level():
    """Gemini : l'effort devient la forme native, et rien pour un cran absent."""
    spec = providers.ProviderSpec(
        name="gemini",
        kind="gemini",
        base_url="https://g.test/v1beta",
        reasoning_style="thinking_level",
        reasoning_map={"low": "low", "high": "high"},
    )
    adaptateur = providers.GeminiProvider(spec)
    if adaptateur._thinking_config("high", ["thinking"]) != {"thinkingLevel": "high"}:
        return False, f"forme native inattendue : {adaptateur._thinking_config('high', ['thinking'])!r}"
    # L'échelle Gemini n'a que deux crans utiles : `medium` n'envoie rien.
    if adaptateur._thinking_config("medium", ["thinking"]) is not None:
        return False, "`medium` traduit alors qu'aucun cran ne lui correspond"

    budget = providers.GeminiProvider(
        providers.ProviderSpec(
            name="gemini",
            kind="gemini",
            base_url="https://g.test/v1beta",
            reasoning_style="thinking_budget",
            reasoning_map={"high": "16384"},
        )
    )._thinking_config("high", ["thinking"])
    if budget != {"thinkingBudget": 16384}:
        return False, f"budget non converti en entier : {budget!r}"

    # De bout en bout : l'effort traverse l'adaptateur jusqu'au transport.
    with patch.object(providers.ai_query, "query_gemini", return_value=("ok", {})) as appel:
        adaptateur.chat(
            "gemini-3.8-flash", "question", effort="high", supported_parameters=["thinking"]
        )
    envoye = appel.call_args.kwargs.get("thinking_config")
    if envoye != {"thinkingLevel": "high"}:
        return False, f"`thinking_config` non transmis au transport : {envoye!r}"
    return True, "effort Gemini traduit en forme native et transmis au transport"


def _cas_effort_gemini_non_declare():
    """Un modèle dont la capacité de réflexion n'est pas écrite ne pense pas."""
    adaptateur = providers.GeminiProvider(
        providers.ProviderSpec(
            name="gemini",
            kind="gemini",
            base_url="https://g.test/v1beta",
            reasoning_style="thinking_level",
            reasoning_map={"low": "low", "high": "high"},
        )
    )
    for capacites, motif in (
        (["tools"], "capacités publiées sans `thinking` (Gemma)"),
        (None, "aucune capacité publiée"),
        ([], "liste de capacités vide"),
    ):
        if adaptateur._thinking_config("high", capacites) is not None:
            return False, f"{motif} : un `thinkingConfig` est parti"

    with patch.object(providers.ai_query, "query_gemini", return_value=("ok", {})) as appel:
        adaptateur.chat(
            "gemma-4-26b-a4b-it", "question", effort="high", supported_parameters=["tools"]
        )
    if appel.call_args.kwargs.get("thinking_config") is not None:
        return False, "`thinking_config` transmis à un modèle non déclaré"
    return True, "aucun `thinkingConfig` vers un modèle dont la capacité n'est pas écrite"


def _cas_kind_inconnu():
    cat = _catalogue(
        {
            "zz": _provider(
                "zz",
                kind="groq",
                keys_env=["GROQ_API_KEY"],
                models=[_model("zz-free-1")],
            )
        }
    )
    res = _run_broker(
        [], catalogue=cat, keys={"GROQ_API_KEY": "k"}, plan=[None], prompt="hello"
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1 : refus propre)"
    if res["appels"]:
        return False, "un appel est parti vers un adaptateur inconnu"
    if "kind inconnu" not in res["stderr"]:
        return False, f"motif absent : {res['stderr']!r}"
    if not res["recus"] or res["recus"][0].get("error_type") != "cascade_exhausted":
        return False, f"reçu={res['recus']}"
    return True, "un `kind` inconnu est un refus explicite, pas une exception"


def _cas_echec_total_reseau():
    res = _run_broker(
        [], catalogue=_seul_gratuit(), keys=_CLES_STD, plan=[_reseau(), _reseau()]
    )
    if res["code"] != 1:
        return False, f"code={res['code']} (attendu 1)"
    if len(res["appels"]) != 2:
        return False, f"{len(res['appels'])} appels (attendu 2 : `per_model` = 2)"
    if not res["recus"]:
        return False, "aucun reçu écrit"
    recu = res["recus"][0]
    if recu.get("error_type") != "cascade_exhausted":
        return False, f"error_type={recu.get('error_type')!r} (attendu cascade_exhausted)"
    if recu.get("attempts") != 2 or recu.get("fallbacks") != 1:
        return False, (
            f"attempts={recu.get('attempts')}, fallbacks={recu.get('fallbacks')}"
        )
    # « Qui a refusé » : une cascade épuisée doit nommer le fournisseur et le
    # modèle fautifs, sans quoi le reçu ne se rattache ni à un quota ni à un
    # disjoncteur, et l'incident reste introuvable au tableau de bord.
    if recu.get("provider") != "or" or recu.get("model") != "or-free-1":
        return False, (
            f"fournisseur={recu.get('provider')!r}, modèle={recu.get('model')!r} "
            "(attendu or / or-free-1)"
        )
    # Ce fournisseur ne publie pas de plafond : le reçu doit dire « je ne sais
    # pas », jamais un chiffre inventé — un plafond inventé écarte des modèles
    # qui fonctionnent. Les deux appels partis, eux, se comptent.
    ecart = _ecart_quota(
        recu, {"window": "daily", "limit": None, "used": 2, "remaining": None}
    )
    if ecart:
        return False, f"reçu d'échec total : {ecart}"
    if any(l.get("error_type") != "api_error" for l in res["ledger"]):
        return False, f"journal={res['ledger']} (une panne réseau n'a pas de statut)"
    return True, "cascade épuisée sur panne réseau : reçu complet, deux lignes d'erreur"


def _cas_recu_quota_restant():
    """Le quota d'un reçu est un compteur vivant, pas une constante décorative.

    Deux passages sur le **même** état : le second doit lire un appel de plus que
    le premier. Un reçu qui afficherait toujours « 49 restants » passerait le cas
    « recu_et_journal » et mentirait au tableau de bord — c'est exactement l'erreur
    que ce cas existe pour rendre impossible.
    """
    usage = _usage_contexte("banc-recu-quota-")
    premier = _run_broker(
        [], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[None], usage_dir=usage
    )
    if premier["code"] != 0 or not premier["recus"]:
        return False, (
            f"1er passage code={premier['code']} stderr={premier['stderr'].strip()!r}"
        )
    ecart = _ecart_quota(
        premier["recus"][0],
        {"window": "daily", "limit": 50, "used": 1, "remaining": 49},
    )
    if ecart:
        return False, f"1er passage : {ecart}"

    second = _run_broker(
        [], catalogue=_std_catalogue(), keys=_CLES_STD, plan=[None], usage_dir=usage
    )
    if second["code"] != 0 or not second["recus"]:
        return False, (
            f"2e passage code={second['code']} stderr={second['stderr'].strip()!r}"
        )
    ecart = _ecart_quota(
        second["recus"][0],
        {"window": "daily", "limit": 50, "used": 2, "remaining": 48},
    )
    if ecart:
        return False, (
            f"2e passage : {ecart} — le quota du reçu ne suit pas les appels déjà "
            "partis dans la fenêtre, il ne sert donc à rien"
        )
    if len(second["ledger"]) != 2:
        return False, (
            f"journal={second['ledger']} : deux appels partis, deux lignes d'usage "
            "— c'est cette égalité qui rend le quota du reçu vérifiable"
        )
    return True, "le quota du reçu décroît avec les appels réellement partis"


def _cas_sante_sur_403_punit_le_modele():
    """Un 403 est une faute de **clé**, jamais de **modèle**.

    `health` documente explicitement que 401/403 relèvent du ledger de clés et
    non du disjoncteur de modèles. Le courtier appelait pourtant
    `record_failure` avec le statut 403 **avant** de regarder
    `exc.provider_fatal`, si bien que le modèle sain partait en quarantaine
    120 s. Comme une seconde clé existe, le prochain appel doit retomber sur ce
    même modèle — et non sur Gemini. Le cas reste comme garde-fou.
    """
    cat = _catalogue(
        {
            "or": _provider(
                "or",
                keys_env=["OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"],
                models=[_model("or-free-1")],
            ),
            "gm": _provider(
                "gm",
                kind="gemini",
                base_url="",
                keys_env=["GEMINI_API_KEY"],
                models=[_model("gm-free-1")],
            ),
        }
    )
    cles = {
        "OPENROUTER_API_KEY": "k1",
        "OPENROUTER_API_KEY_2": "k2",
        "GEMINI_API_KEY": "kg",
    }
    dossier = tempfile.mkdtemp(prefix="banc-broker-403-")
    _TEMP_DIRS.append(dossier)

    premier = _run_broker(
        [], catalogue=cat, keys=cles, plan=[_http(403), None], usage_dir=dossier
    )
    if premier["code"] != 0 or len(premier["appels"]) != 2:
        return False, f"première passe inattendue : code={premier['code']}"
    if premier["appels"][1]["provider"] != "gm":
        return False, "la première passe n'a pas replié sur Gemini"

    second = _run_broker([], catalogue=cat, keys=cles, plan=[None], usage_dir=dossier)
    if second["code"] != 0:
        return False, f"code={second['code']} stderr={second['stderr'].strip()!r}"
    if not second["appels"]:
        return False, "aucun appel lors de la seconde passe"
    choisi = second["appels"][0]
    if choisi["provider"] != "or":
        return False, (
            f"deuxième passe servie par « {choisi['provider']} » : le modèle "
            "or-free-1 est toujours en cooldown pour une faute de clé, alors "
            "qu'une clé saine attend (OPENROUTER_API_KEY_2)"
        )
    # Le cooldown se lit sur la première passe : la seconde, servie par ce même
    # modèle, l'effacerait par un succès.
    restant = _cooldown_restant(premier, "or", "or-free-1")
    if restant is not None and restant > 0:
        return False, (
            f"le 403 de clé a écarté le modèle or-free-1 ({restant:.0f} s de "
            "cooldown restants) : une faute de clé n'est pas une faute de modèle"
        )
    # Le journal est relu **en entier** depuis le disque : la ligne de la seconde
    # passe est donc la dernière, pas la première.
    cle_utilisee = second["ledger"][-1].get("key_id")
    if cle_utilisee != "OPENROUTER_API_KEY_2":
        return False, f"clé utilisée={cle_utilisee!r}"
    return True, "un 403 de clé ne doit pas écarter le modèle : clé saine réutilisée"


def _cas_transcribe_non_conversationnel():
    """Un modèle de transcription n'est pas un modèle de conversation.

    Défaut trouvé par ce banc, corrigé depuis dans `refresh_models` : le cas
    reste pour empêcher le marqueur de disparaître au prochain nettoyage.
    """
    identifiant = "gemini-3.5-transcribe"
    brut = {"id": identifiant, "supportedGenerationMethods": ["generateContent"]}
    capable = refresh_models._text_capable(brut, identifiant)
    if capable:
        return False, (
            f"« {identifiant} » est accepté comme modèle de conversation : "
            "ajouter « transcribe » à `_NON_CHAT_MARKERS`"
        )
    return True, "un modèle de transcription est écarté du catalogue"


# ---------------------------------------------------------------------------
# Définition des cas
# ---------------------------------------------------------------------------

CASES = [
    # --- FLOOR : comportement nominal, à ne jamais casser ---
    {
        "id": "succes_premier_candidat",
        "kind": "floor",
        "description": "Catalogue sain → premier candidat gratuit servi, santé effacée",
    },
    {
        "id": "gratuit_avant_payant",
        "kind": "floor",
        "description": "Payant déclaré en premier et mieux classé → le gratuit passe quand même",
    },
    {
        "id": "repli_sur_400",
        "kind": "floor",
        "description": "400 → modèle suivant, sans cooldown (la requête était en cause)",
    },
    {
        "id": "repli_apres_429",
        "kind": "floor",
        "description": "429 → même modèle retenté, puis suivant, cooldown doublé",
    },
    {
        "id": "retry_after_honore",
        "kind": "floor",
        "description": "Retry-After 7 s → cooldown ~8 s, pas le backoff de 240 s",
    },
    {
        "id": "modele_explicite",
        "kind": "floor",
        "description": "`-m` → un seul candidat, le routage est court-circuité",
    },
    {
        "id": "recu_et_journal",
        "kind": "floor",
        "description": "Un reçu complet et une ligne de journal par appel réussi",
    },
    {
        "id": "liste_candidats_sans_appel",
        "kind": "floor",
        "description": "`--list-candidates` expose le routage sans appeler ni tracer",
    },
    {
        "id": "output_ecrit_le_fichier",
        "kind": "floor",
        "description": "`--raw-code -o` écrit le bloc Python seul, sans stdout",
    },
    {
        "id": "rotation_deux_cles",
        "kind": "floor",
        "description": "Deux clés → la moins sollicitée de la fenêtre passe en second",
    },
    {
        "id": "enveloppe_extrait_le_code",
        "kind": "floor",
        "description": "Mode `code` : enveloppe JSON lue, seul le champ `code` écrit",
    },
    {
        "id": "contexte_pave_et_cache",
        "kind": "floor",
        "description": "Mode `context` : deux documents en un appel, puis zéro requête",
    },
    {
        "id": "recherche_locale_sans_appel",
        "kind": "floor",
        "description": "Mode `search` : un fichier sans concurrent → réponse locale, zéro appel",
    },
    {
        "id": "effort_traduit_par_fournisseur",
        "kind": "floor",
        "description": "`--effort` : graphie du fournisseur, filtrée par les capacités, sinon rien",
    },
    {
        "id": "effort_gemini_thinking_level",
        "kind": "floor",
        "description": "Gemini : effort traduit en `thinkingLevel`, `medium` n'envoie rien",
    },
    # --- ADVERSARIAL : cas limites et défauts figés par la mesure ---
    {
        "id": "sortie_extraction_vide",
        "kind": "adversarial",
        "description": "Texte non vide mais aucun code extrait → échec immédiat, tracé",
    },
    {
        "id": "reponse_vide_persiste_quota",
        "kind": "adversarial",
        "description": "Réponse vide → le quota reste consommé, l'erreur est au journal",
    },
    {
        "id": "syntaxe_sortie_3",
        "kind": "adversarial",
        "description": "`--check-syntax` sur du code cassé → code 3 et ligne d'erreur",
    },
    {
        "id": "enveloppe_repare_la_syntaxe",
        "kind": "adversarial",
        "description": "Code cassé → l'erreur exacte repart au modèle, qui corrige",
    },
    {
        "id": "enveloppe_reparation_epuisee",
        "kind": "adversarial",
        "description": "Réparation insuffisante → échec explicite en 3, jamais une boucle",
    },
    {
        "id": "syntaxe_cascade_vers_modele_suivant",
        "kind": "adversarial",
        "description": "Code cassé + réparation épuisée → cascade vers le modèle suivant qui réussit",
    },
    {
        "id": "syntaxe_cascade_tous_epuises",
        "kind": "adversarial",
        "description": "Tous les modèles en syntaxe cassée → code 3 propagé, pas 1",
    },
    {
        "id": "enveloppe_absente_repli",
        "kind": "adversarial",
        "description": "Modèle qui ignore l'enveloppe → repli sur l'extraction historique",
    },
    {
        "id": "quarantaine_403",
        "kind": "adversarial",
        "description": "403 → clé en quarantaine, fournisseur écarté, autre fournisseur",
    },
    {
        "id": "modele_retire_404",
        "kind": "adversarial",
        "description": "404 → modèle marqué retiré, jamais reproposé tant qu'il y est",
    },
    {
        "id": "modele_en_cooldown",
        "kind": "adversarial",
        "description": "Modèle en cooldown → écarté et motif publié par `--list-candidates`",
    },
    {
        "id": "cle_rejetee",
        "kind": "adversarial",
        "description": "Clé en quarantaine → refus explicite, aucun appel émis",
    },
    {
        "id": "quota_fenetre_atteinte",
        "kind": "adversarial",
        "description": "Quota de fenêtre atteint → refus explicite, aucun appel émis",
    },
    {
        "id": "fenetre_changee",
        "kind": "adversarial",
        "description": "Compteur de la fenêtre précédente → remis à zéro, appel possible",
    },
    {
        "id": "payant_refuse_sans_autorisation",
        "kind": "adversarial",
        "description": "Payant sans drapeau → refus motivé, jamais appelé en silence",
    },
    {
        "id": "payant_non_valide",
        "kind": "adversarial",
        "description": "Payant hors allowlist → refus, même avec `--allow-paid`",
    },
    {
        "id": "payant_prix_inconnu",
        "kind": "adversarial",
        "description": "Prix illisible → refus, jamais traité comme gratuit",
    },
    {
        "id": "payant_prix_absent",
        "kind": "adversarial",
        "description": "Prix absent (clé manquante, zéros fantômes, champs nuls) → refus",
    },
    {
        "id": "plafond_zero",
        "kind": "adversarial",
        "description": "Plafond non déclaré (0 $) → aucun appel payant possible",
    },
    {
        "id": "plafond_depasse",
        "kind": "adversarial",
        "description": "Coût estimé au-dessus du plafond → refus avant envoi",
    },
    {
        "id": "escalade_autorise_payant",
        "kind": "adversarial",
        "description": "`--escalate` sous plafond → appel payant autorisé et imputé",
    },
    {
        "id": "mode_raisonnement_payant",
        "kind": "adversarial",
        "description": "Mode `reasoning` déclaré payant → autorisé sans drapeau",
    },
    {
        "id": "masquage_secret",
        "kind": "adversarial",
        "description": "Clé dans le prompt → masquée dans le corps HTTP et comptée",
    },
    {
        "id": "json_mode_transmis",
        "kind": "adversarial",
        "description": "`--json-output` transmis à l'adaptateur, et seulement là",
    },
    {
        "id": "response_format_capacites",
        "kind": "adversarial",
        "description": "`response_format` omis quand le modèle ne le déclare pas",
    },
    {
        "id": "json_schema_natif_envoye",
        "kind": "adversarial",
        "description": "Capacité déclarée → contrat strict `json_schema` sur le fil, relance comprise",
    },
    {
        "id": "json_schema_strict_ou_absent",
        "kind": "adversarial",
        "description": "`json_schema` strict si déclaré, `json_object` sinon, rien sans demande",
    },
    {
        "id": "json_reparation_refusee",
        "kind": "adversarial",
        "description": "`--max-repairs 0` → échec explicite au premier dérapage, sans relance",
    },
    {
        "id": "kind_inconnu",
        "kind": "adversarial",
        "description": "`kind` inconnu au catalogue → refus propre, pas d'exception",
    },
    {
        "id": "echec_total_reseau",
        "kind": "adversarial",
        "description": "Panne réseau sur tout le catalogue → cascade_exhausted chiffrée",
    },
    {
        "id": "transcribe_non_conversationnel",
        "kind": "adversarial",
        "description": "Un modèle de transcription reste hors du catalogue textuel",
    },
    {
        "id": "sante_sur_403_punit_le_modele",
        "kind": "adversarial",
        "description": "Un 403 de clé n'écarte pas le modèle : clé saine réutilisée",
    },
    {
        "id": "contexte_partiel",
        "kind": "adversarial",
        "description": "Document déjà indexé non renvoyé, réponse en désordre rangée",
    },
    {
        "id": "contexte_json_invalide",
        "kind": "adversarial",
        "description": "Réponse de pavage illisible → code 4, rien de rangé au cache",
    },
    {
        "id": "contexte_repare_le_json",
        "kind": "adversarial",
        "description": "Pavage illisible puis relance conforme → rangé au cache, zéro relance au tour suivant",
    },
    {
        "id": "contexte_cache_invalide",
        "kind": "adversarial",
        "description": "`--no-cache` n'écrit pas, un caractère changé périme l'entrée",
    },
    {
        "id": "recherche_desambiguisee",
        "kind": "adversarial",
        "description": "Égalité de score → un seul appel, choix contraint à la liste locale",
    },
    {
        "id": "recherche_choix_invalide",
        "kind": "adversarial",
        "description": "Chemin hors liste fermée → code 4, ligne d'erreur, rien de rendu",
    },
    {
        "id": "recherche_repare_le_json",
        "kind": "adversarial",
        "description": "Choix hors liste puis relance dans la liste → désambiguïsation aboutie",
    },
    {
        "id": "recherche_aucun_resultat",
        "kind": "adversarial",
        "description": "Aucune correspondance locale → échec en 1, zéro appel",
    },
    {
        "id": "effort_gemini_non_declare",
        "kind": "adversarial",
        "description": "Capacité de réflexion non déclarée → aucun `thinkingConfig` envoyé",
    },
    {
        "id": "recu_quota_restant",
        "kind": "adversarial",
        "description": "Deux appels sur le même état → le quota du reçu passe de 49 à 48 restants",
    },
    {
        "id": "provider_filtre_avant_troncature",
        "kind": "adversarial",
        "description": "`--provider X` filtre avant `max_candidates`, et refuse un fournisseur absent",
    },
]

_CHECKS = {
    "succes_premier_candidat": _cas_succes_premier_candidat,
    "gratuit_avant_payant": _cas_gratuit_avant_payant,
    "repli_sur_400": _cas_repli_sur_400,
    "repli_apres_429": _cas_repli_apres_429,
    "retry_after_honore": _cas_retry_after_honore,
    "modele_explicite": _cas_modele_explicite,
    "recu_et_journal": _cas_recu_et_journal,
    "liste_candidats_sans_appel": _cas_liste_candidats_sans_appel,
    "output_ecrit_le_fichier": _cas_output_ecrit_le_fichier,
    "rotation_deux_cles": _cas_rotation_deux_cles,
    "enveloppe_extrait_le_code": _cas_enveloppe_extrait_le_code,
    "sortie_extraction_vide": _cas_sortie_extraction_vide,
    "reponse_vide_persiste_quota": _cas_reponse_vide_persiste_quota,
    "syntaxe_sortie_3": _cas_syntaxe_sortie_3,
    "enveloppe_repare_la_syntaxe": _cas_enveloppe_repare_la_syntaxe,
    "enveloppe_reparation_epuisee": _cas_enveloppe_reparation_epuisee,
    "syntaxe_cascade_vers_modele_suivant": _cas_syntaxe_cascade_vers_modele_suivant,
    "syntaxe_cascade_tous_epuises": _cas_syntaxe_cascade_tous_epuises,
    "enveloppe_absente_repli": _cas_enveloppe_absente_repli,
    "contexte_pave_et_cache": _cas_contexte_pave_et_cache,
    "contexte_partiel": _cas_contexte_partiel,
    "contexte_json_invalide": _cas_contexte_json_invalide,
    "contexte_repare_le_json": _cas_contexte_repare_le_json,
    "contexte_cache_invalide": _cas_contexte_cache_invalide,
    "recherche_locale_sans_appel": _cas_recherche_locale_sans_appel,
    "recherche_desambiguisee": _cas_recherche_desambiguisee,
    "recherche_choix_invalide": _cas_recherche_choix_invalide,
    "recherche_repare_le_json": _cas_recherche_repare_le_json,
    "recherche_aucun_resultat": _cas_recherche_aucun_resultat,
    "quarantaine_403": _cas_quarantaine_403,
    "modele_retire_404": _cas_modele_retire_404,
    "modele_en_cooldown": _cas_modele_en_cooldown,
    "cle_rejetee": _cas_cle_rejetee,
    "quota_fenetre_atteinte": _cas_quota_fenetre_atteinte,
    "fenetre_changee": _cas_fenetre_changee,
    "payant_refuse_sans_autorisation": _cas_payant_refuse_sans_autorisation,
    "payant_non_valide": _cas_payant_non_valide,
    "payant_prix_inconnu": _cas_payant_prix_inconnu,
    "payant_prix_absent": _cas_payant_prix_absent,
    "plafond_zero": _cas_plafond_zero,
    "plafond_depasse": _cas_plafond_depasse,
    "escalade_autorise_payant": _cas_escalade_autorise_payant,
    "mode_raisonnement_payant": _cas_mode_raisonnement_payant,
    "provider_filtre_avant_troncature": _cas_provider_filtre_avant_troncature,
    "masquage_secret": _cas_masquage_secret,
    "json_mode_transmis": _cas_json_mode_transmis,
    "response_format_capacites": _cas_response_format_capacites,
    "json_schema_natif_envoye": _cas_json_schema_natif_envoye,
    "json_schema_strict_ou_absent": _cas_json_schema_strict_ou_absent,
    "json_reparation_refusee": _cas_json_reparation_refusee,
    "effort_traduit_par_fournisseur": _cas_effort_traduit_par_fournisseur,
    "effort_gemini_thinking_level": _cas_effort_gemini_thinking_level,
    "effort_gemini_non_declare": _cas_effort_gemini_non_declare,
    "recu_quota_restant": _cas_recu_quota_restant,
    "kind_inconnu": _cas_kind_inconnu,
    "echec_total_reseau": _cas_echec_total_reseau,
    "transcribe_non_conversationnel": _cas_transcribe_non_conversationnel,
    "sante_sur_403_punit_le_modele": _cas_sante_sur_403_punit_le_modele,
}


def run_case(case):
    """Exécute un cas et renvoie (ok: bool, detail: str)."""
    controle = _CHECKS.get(case["id"])
    if controle is None:
        return False, f"cas inconnu : {case['id']}"
    return controle()


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
    try:
        for case in CASES:
            try:
                ok, detail = run_case(case)
            except Exception as exc:
                ok, detail = False, f"exception inattendue : {exc!r}"
            results.append((case, ok, detail))
    finally:
        for dossier in _TEMP_DIRS:
            shutil.rmtree(dossier, ignore_errors=True)
        _TEMP_DIRS.clear()

    n_ok = sum(1 for _, ok, _ in results if ok)

    if args.json:
        out = {"script": "ai_broker", "failed": []}
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
