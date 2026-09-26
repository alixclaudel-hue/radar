#!/usr/bin/env python3
"""Passerelle multi-fournisseurs — point d'entrée unique de la délégation.

Ce script remplace `scripts/ai_query.py` comme **porte d'entrée**, sans le
remplacer comme **transport** : l'adaptateur Gemini du socle appelle toujours
`ai_query.query_gemini()`, qui reste le code testé du protocole Gemini. Le
contrat de ligne de commande est identique (mêmes options, mêmes codes de retour,
mêmes noms de champs de reçu), donc rien de ce qui appelle `ai_query.py`
aujourd'hui n'a besoin d'être réécrit.

Ce que ce courtier ajoute :

- **le choix du fournisseur** par le routeur ([`router.order_candidates()`](scripts/ai/router.py:1)) :
  gratuit d'abord, payant seulement si validé *et* autorisé ;
- **la tentative successive** : un modèle qui échoue durablement est écarté
  ([`health`](scripts/ai/health.py:1)) et le candidat suivant prend la main, au
  lieu de brûler six modèles de la même liste comme la version mono-Gemini ;
- **la comptabilité** : chaque tentative réellement partie laisse une ligne dans
  `ai_usage.jsonl`, ce qui rend la consommation mesurable au lieu d'estimée ;
- **la réparation** : en mode `code`/`test`, la réponse est une enveloppe JSON
  (`explication`, `code`) dont le code est compilé *avant* d'être écrit ; s'il ne
  compile pas, l'erreur exacte repart au modèle ([`envelope.repair_prompt()`](scripts/ai/envelope.py:1)),
  jusqu'à `--max-repairs` tours (défaut 1) avant l'abandon en code 3 ;
- **la garantie JSON** : les modes à sortie structurée demandent au fournisseur son
  format natif quand il le déclare ([`schema`](scripts/ai/schema.py:1)), relisent la
  réponse contre le schéma, et la renvoient **une** fois au modèle avec le défaut
  constaté si elle ne tient pas. Un JSON à moitié valide n'atteint jamais le disque ;
- **le pavage de contexte** : en mode `context`, plusieurs documents partent en
  **un seul appel** et un index disque indexé par empreinte évite de relire deux
  fois le même fichier ([`context_cache`](scripts/ai/context_cache.py:1)). Un
  document déjà analysé ne repart jamais au modèle ;
- **la recherche locale** : en mode `search`, l'arborescence est parcourue et
  classée sur place ([`locate`](scripts/ai/locate.py:1)). Le cas nominal ne coûte
  **aucune requête** ; un appel n'a lieu que si deux fichiers distincts se
  disputent le premier rang, et le modèle n'a alors qu'à désigner un chemin dans
  une liste fermée ;
- **le masquage** : le prompt est nettoyé dans l'adaptateur, donc aucun chemin de
  ce script ne peut envoyer une clé par accident.

Quatre codes de retour : `0` succès, `1` échec, `3` syntaxe invalide sous
`--check-syntax`, `4` sortie JSON inexploitable (`context`, `search`,
`--json-output`). Les trois premiers sont ceux d'`ai_query.py` ; le quatrième est
propre aux modes à sortie structurée et permet à un appelant shell de distinguer
« le modèle a répondu n'importe quoi » d'un échec de transport. Le hook
`scripts/hooks/gemini_gate.py` lit le reçu, pas le code de retour — mais un
appelant shell, si.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Lancé en direct (`python3 scripts/ai_broker.py`), `sys.path[0]` vaut `scripts/`
# et le socle `scripts.ai` serait introuvable. Les skills et le hook appellent ce
# script par son chemin de fichier : la racine du dépôt doit donc être importable
# d'elle-même. Même amorçage que `scripts/bench_ai_query.py`.
_RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RACINE not in sys.path:
    sys.path.insert(0, _RACINE)

from scripts import ai_query  # noqa: E402
from scripts.ai import catalogue as cat_mod  # noqa: E402
from scripts.ai import (  # noqa: E402
    context_cache,
    envelope,
    health,
    jsonout,
    locate,
    prompts,
    providers,
    quota,
    router,
    schema,
)

RECEIPT_SOURCE_DEFAULT = "ai_broker"

# Erreur synthétique quand tous les candidats ont été épuisés : c'est le nom que
# l'ancienne cascade utilisait, et le tableau de bord le lit déjà.
ERROR_CASCADE_EXHAUSTED = "cascade_exhausted"


def _sleep(seconds: float) -> None:
    """Isolé au niveau module pour que le banc n'attende pas réellement."""
    time.sleep(seconds)


def _noop(_seconds: float) -> None:
    """Substitut de `_sleep` utilisé quand l'attente est désactivée."""
    return None


def _retry_delay(attempt: int, policy: dict) -> float:
    """Backoff exponentiel du catalogue, borné par `max_delay`."""
    retry = policy.get("retry") or {}
    try:
        base = float(retry.get("base_delay") or 4.0)
        plafond = float(retry.get("max_delay") or 30.0)
    except (TypeError, ValueError):
        base, plafond = 4.0, 30.0
    return min(plafond, base * (2 ** max(0, int(attempt) - 1)))


def _resolve_key(candidate: router.Candidate, key_lookup) -> str | None:
    """Valeur de la clé du candidat, ou `None` si l'appel part sans clé."""
    if not candidate.key_env:
        return None
    return key_lookup(candidate.key_env)


def _status_of(exc: Exception) -> int | None:
    statut = getattr(exc, "status", None)
    if statut is None:
        return None
    try:
        return int(statut)
    except (TypeError, ValueError):
        return None


def _error_type(exc: Exception) -> str:
    statut = _status_of(exc)
    if statut is not None:
        return f"http_{statut}"
    return "api_error"


def _charge_paid(completion, candidate: router.Candidate, state: dict, now: float | None) -> float:
    """Impute au plafond le coût réel d'un appel payant, d'après les jetons rapportés."""
    if candidate.free:
        return 0.0
    usage = completion.usage or {}
    cout = cat_mod.cost_estimate(
        candidate.entry,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )
    quota.charge_paid(state, cout, now)
    return cout


def _syntax_error(code: str, nom: str = "<broker-output>") -> SyntaxError | None:
    """`SyntaxError` du code, ou `None` s'il compile.

    Le nom passé à `compile()` est celui d'`ai_query.py` : le message d'erreur
    garde la forme que les appelants connaissent déjà.
    """
    try:
        compile(code, nom, "exec")
    except SyntaxError as erreur:
        return erreur
    return None


def _extraire_sortie(
    texte: str, *, enveloppe: bool, raw_code: bool, mode: str
) -> tuple[str, str]:
    """`(réponse, explication)` selon le mode — une seule définition.

    Le premier jet et la relance doivent lire leur réponse **de la même façon** :
    tant que la boucle de réparation ne servait qu'aux modes de code, la lecture
    était écrite en clair deux fois, dont une dans la relance. Depuis qu'elle sert
    aussi aux contrats JSON, la seconde aurait lu avec `split_answer` une réponse de
    mode `context`, où il n'y a jamais d'enveloppe — un bug que rien n'aurait vu.
    """
    if enveloppe:
        # Le repli est *dans* `split_answer` : un modèle qui rend un bloc markdown
        # classique reste utilisable, avec une explication vide.
        return envelope.split_answer(texte)
    reponse = texte
    if raw_code or mode in prompts.CODE_LIKE_MODES:
        reponse = prompts.extract_raw_code(reponse)
    return reponse, ""


def _lisible_json(texte: str):
    """Charge JSON de la réponse, ou `None` — objet **ou** tableau.

    [`jsonout.extract_json_object()`](scripts/ai/jsonout.py:102) ne rend que des
    dictionnaires : parfait pour les contrats nommés, insuffisant pour
    `--json-output`, qui demande « du JSON » et n'a aucune raison de refuser un
    tableau à la racine.
    """
    brut = (texte or "").strip()
    if not brut:
        return None
    try:
        return json.loads(brut)
    except ValueError:
        pass
    return jsonout.extract_json_object(texte)


def _contrat_json(args, contexte, recherche, enveloppe: bool):
    """`(nom, schéma)` du contrat JSON attendu, ou `None` s'il n'y en a pas.

    L'ordre dit la priorité : le pavage de contexte, puis le choix de recherche,
    puis l'enveloppe des modes de code, puis le JSON nu de `--json-output`. Un
    schéma `None` veut dire « du JSON, sans plus de forme » — le contrat le plus
    faible, et le seul où un tableau à la racine est accepté.
    """
    if contexte is not None:
        return schema.CONTEXTE_NOM, schema.CONTEXTE
    if recherche is not None:
        return schema.RECHERCHE_NOM, schema.RECHERCHE
    if enveloppe:
        return schema.ENVELOPPE_NOM, schema.ENVELOPPE
    if args.json_mode:
        return "json", None
    return None


def _prompt_reparation_json(demande: str, reponse: str, detail: str) -> str:
    """Relance d'une sortie qui ne respecte pas son contrat JSON.

    La demande d'origine est **recopiée** : chaque appel est indépendant, le modèle
    ne se souvient de rien, et il ne peut ni refaire l'analyse de documents qu'il ne
    voit plus, ni choisir dans une liste qu'il a oubliée. C'est plus cher en jetons —
    c'est le prix d'une réparation qui peut aboutir, et il n'y en a qu'une.

    La réponse fautive est recopiée aussi, bornée : c'est elle qui montre au modèle
    où son format a dérapé, au lieu de le laisser deviner.
    """
    return (
        f"{demande}\n\n"
        "---\n"
        "Ta réponse précédente ne respecte pas le format demandé.\n"
        f"Défaut constaté : {detail}\n\n"
        "Réponse fautive :\n"
        "```\n"
        f"{reponse.strip()[:4000]}\n"
        "```\n\n"
        "Rends de nouveau un **unique objet JSON** conforme au format demandé, "
        "portant sur la demande rappelée ci-dessus. Rien avant, rien après."
    )


def _probleme_json(detail: str, reponse: str, demande: str) -> dict:
    """Défaut de contrat JSON — même forme pour tous les cas, code 4."""
    return {
        "type": "invalid_json",
        "detail": detail,
        "code": 4,
        "prompt": _prompt_reparation_json(demande, reponse, detail),
    }


def _probleme_sortie(
    reponse: str,
    *,
    verifier_syntaxe: bool,
    contrat,
    contexte,
    recherche,
    demande: str,
) -> dict | None:
    """Le premier défaut qui rend la sortie inexploitable, ou `None` si elle tient.

    Rend `{"type", "detail", "code", "prompt"}` : de quoi écrire la ligne d'erreur,
    le reçu et la relance, sans que la boucle ait à savoir de quel défaut il s'agit.

    La syntaxe passe avant le JSON : un mode de code dont le programme ne compile
    pas doit sortir en 3, pas en 4 — sinon l'appelant lit « JSON inexploitable »
    pour un problème de Python.

    La relecture va jusqu'où le contrat va : structure du schéma, puis les deux
    vérifications que seul le courtier peut faire parce que seule la demande les
    connaît — les documents rendus sont-ils ceux envoyés, le chemin choisi est-il
    dans la liste fermée. Un chemin **vide** n'est pas un défaut : le prompt de
    `search` autorise explicitement « aucun candidat ne convient », et c'est au mode
    de le traduire en code 4, pas à la relecture de renvoyer la balle au modèle.
    """
    if verifier_syntaxe:
        erreur = _syntax_error(reponse)
        if erreur is not None:
            return {
                "type": "syntax_error",
                "detail": str(erreur),
                "code": 3,
                "prompt": envelope.repair_prompt(reponse, erreur),
            }
    if contrat is None:
        return None

    nom, attendu = contrat
    if attendu is None:
        if _lisible_json(reponse) is None:
            detail = "aucun JSON lisible dans la réponse"
            return _probleme_json(detail, reponse, demande)
        return None

    charge = jsonout.extract_json_object(reponse)
    if charge is None:
        detail = f"aucun objet JSON lisible alors que le contrat « {nom} » l'exige"
        return _probleme_json(detail, reponse, demande)
    erreurs = schema.valider(charge, attendu)
    if erreurs:
        detail = f"contrat « {nom} » non respecté : {schema.resume(erreurs)}"
        return _probleme_json(detail, reponse, demande)

    if contexte is not None and context_cache.apparier(charge, contexte["a_analyser"]) is None:
        detail = (
            "les documents rendus ne correspondent pas à ceux envoyés : un élément "
            "par document, dans l'ordre, chacun avec son `path`"
        )
        return _probleme_json(detail, reponse, demande)

    if recherche is not None:
        choix = str(charge.get("path") or "").strip()
        connus = {
            os.path.normpath(hit["path"]) for hit in (locate.ambigue(recherche) or [])
        }
        if choix and os.path.normpath(choix) not in connus:
            detail = f"choix hors de la liste des candidats : {choix!r}"
            return _probleme_json(detail, reponse, demande)
    return None


def _appeler(
    candidat: router.Candidate,
    prompt: str,
    *,
    system_instruction: str,
    is_json: bool,
    effort: str | None,
    valeur_cle: str | None,
    json_schema: dict | None = None,
    schema_name: str = "reponse",
):
    """Un appel au fournisseur du candidat, sans aucune comptabilité.

    Extraite pour que la boucle de réparation emprunte **le même** chemin que le
    premier jet : une relance qui passerait par un autre chemin perdrait le
    masquage des secrets, le budget d'effort ou les capacités publiées, et rien
    dans le banc ne le verrait.
    """
    adaptateur = providers.build_provider(candidat.spec)
    return adaptateur.chat(
        model=candidat.model,
        prompt=prompt,
        system=system_instruction,
        json_mode=is_json,
        effort=effort,
        api_key=valeur_cle,
        timeout=(candidat.spec.timeout if candidat.spec else None),
        # Le contrat de sortie voyage avec chaque appel, relance comprise :
        # l'adaptateur décide seul s'il peut le traduire en format natif.
        json_schema=json_schema,
        schema_name=schema_name,
        # Capacités publiées par le fournisseur pour CE modèle, quand il les
        # publie : l'adaptateur s'en sert pour ne pas envoyer un champ que le
        # modèle refuse (400 garanti, appel gratuit perdu).
        supported_parameters=candidat.entry.get("supports"),
    )


def _enregistrer_echec_appel(
    exc: Exception,
    *,
    candidat: router.Candidate,
    key_id: str,
    health_state: dict,
    quota_state: dict,
    mode: str,
    tier: str,
    source: str,
    ts: float,
    attempt_index: int,
    fournisseurs_morts: set[str],
    repair_round: int | None = None,
) -> tuple[int | None, str]:
    """Comptabilise un appel en échec : disjoncteur, quarantaine, journal.

    Rend `(statut_http, motif)`. Utilisé **aussi bien** par la boucle de candidats
    que par la boucle de réparation : sans ce facteur commun, la relance aurait sa
    propre comptabilité et les deux finiraient par diverger — exactement le genre
    d'écart qui fausse un taux de succès mesuré.

    Le partage 401/403 contre le reste vient de la base de mesure : un refus
    d'authentification est une faute de **clé**, il se règle dans le ledger de
    clés et jamais par un cooldown de modèle.
    """
    statut = _status_of(exc)
    motif = str(exc)
    if getattr(exc, "provider_fatal", False):
        # La clé est en cause, pas le modèle : c'est le ledger de clés qui
        # tranche, jamais le disjoncteur de modèles. Poser ici un échec de modèle
        # donnerait 120 s de cooldown à un modèle parfaitement sain, alors qu'une
        # seconde clé du même fournisseur attend d'être utilisée. On écarte la clé,
        # et le fournisseur pour ce tour. C'est le correctif direct des 5
        # `http_403` de la base de mesure.
        quota.mark_key_bad(quota_state, candidat.provider, key_id)
        fournisseurs_morts.add(candidat.provider)
    else:
        health.record_failure(
            health_state,
            candidat.provider,
            candidat.model,
            status=statut,
            retry_after=getattr(exc, "retry_after", None),
            base=health.BASE_COOLDOWN,
            max_delay=health.MAX_COOLDOWN,
            detail=motif,
        )

    ligne = {
        "ts": ts,
        "provider": candidat.provider,
        "model": candidat.model,
        "key_id": key_id,
        "mode": mode,
        "tier": tier,
        "free": candidat.free,
        "paid": not candidat.free,
        "status": "error",
        "error_type": _error_type(exc),
        "status_code": statut,
        "attempt_index": attempt_index,
        "elapsed_ms": round((time.time() - ts) * 1000),
        "source": source,
    }
    if repair_round is not None:
        ligne["repair_round"] = repair_round
    quota.append_ledger(ligne)

    if not candidat.free:
        # Un fournisseur payant facture les jetons d'entrée même quand il refuse :
        # on impute l'estimation, jamais zéro.
        quota.charge_paid(quota_state, candidat.cost_usd, time.time())

    return statut, motif


def _etat_quota(
    catalogue: dict,
    quota_state: dict,
    policy: dict,
    *,
    provider: str | None = None,
    key_id: str | None = None,
    now: float | None = None,
) -> dict:
    """Photo du quota au moment du reçu — ce qui reste, pas ce qui est déclaré.

    Le reçu ne portait que `prompt_chars` et un coût : impossible d'y lire combien
    d'appels gratuits il restait dans la fenêtre du fournisseur, donc impossible de
    distinguer un refus de quota d'un modèle cassé. Ce bloc comble ce trou, et il
    est **le même** sur les chemins de succès, d'échec et de service sans appel —
    sans quoi le tableau de bord comparerait des reçus qui ne se lisent pas avec la
    même grille.

    `limit` (donc `remaining`) vaut `None` quand le fournisseur ne publie pas de
    plafond : on ne l'invente pas, un plafond inventé écarte des modèles qui
    fonctionnent. Le compteur payant, lui, se lit toujours, parce que c'est la seule
    mesure opposable au plafond du jour.
    """
    etat: dict = {}
    if provider is not None:
        spec = cat_mod.quota_spec(catalogue, provider)
        utilise = quota.count_in_window(
            quota_state, provider, key_id or "sans-cle", spec["window"], now=now
        )
        limite = spec.get("limit")
        etat.update(
            window=spec["window"],
            limit=limite,
            used=utilise,
            remaining=None if limite is None else max(0, limite - utilise),
        )
    cap = quota.paid_cap_usd(policy)
    depense = quota.paid_spent(quota_state, now=now)
    etat.update(
        paid_spent_usd=round(depense, 6),
        paid_cap_usd=cap,
        paid_remaining_usd=round(max(0.0, cap - depense), 6),
    )
    return etat


def _build_prompt(args) -> str:
    """Assemble le prompt : instruction, fichiers injectés, entrée standard.

    Reprise à l'identique de `ai_query.main()` — y compris la numérotation des
    lignes en mode `read`/`context`, qui existe pour qu'une réponse puisse citer
    une ligne exacte plutôt qu'une approximation. En mode `context`, c'est
    **cette** numérotation qui sert d'empreinte au cache : les deux côtés
    appellent `prompts.numbered_lines()`, donc la clé décrit exactement le texte
    envoyé.
    """
    parts = []
    if args.prompt:
        parts.append(args.prompt)

    for filepath in args.file:
        if not os.path.isfile(filepath):
            sys.stderr.write(f"Erreur : fichier introuvable '{filepath}'\n")
            return ""
        with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
            contenu = handle.read()
        if args.mode in ("read", "context"):
            contenu = prompts.numbered_lines(contenu)
        parts.append(f"\n--- Fichier : {filepath} ---\n" + contenu)

    if args.stdin or (not sys.stdin.isatty() and not args.prompt and not args.file):
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            parts.append("\n--- Entrée standard ---\n" + stdin_content)

    return "\n\n".join(parts).strip()


def _servir_contexte_du_cache(
    args, contexte: dict, mode: str, tier: str, etat_quota: dict
) -> int:
    """Sortie rendue entièrement par le cache : aucune requête n'est partie.

    Le journal d'usage compte des **appels partis**. Y écrire une ligne pour un
    service rendu par le cache fausserait la seule mesure qui dit combien de
    requêtes coûte la délégation ; le reçu, lui, porte `cached` et `calls: 0`,
    ce qui suffit au tableau de bord comme au banc.
    """
    reponse = context_cache.assembler(contexte["documents"])
    if args.output:
        with open(args.output, "w", encoding="utf-8") as out_f:
            out_f.write(reponse + "\n")
        sys.stderr.write(
            f"[ai_broker] Écrit dans {args.output} depuis le cache de contexte\n"
        )
    else:
        print(reponse)
    ai_query.write_receipt(
        mode,
        "ok",
        output=args.output,
        tier=tier,
        cached=True,
        calls=0,
        documents=len(contexte["documents"]),
        paved=0,
        prompt_chars=0,
        cost_usd=0.0,
        quota=etat_quota,
        source=args.source,
        session=os.environ.get("CLAUDE_SESSION_ID"),
    )
    return 0


def _finaliser_contexte(
    reponse: str,
    contexte: dict,
    *,
    ligne: dict,
    meta: dict,
    cout_total: float,
    mode: str,
    args,
    quota_state: dict,
    health_state: dict,
):
    """Valide le JSON du pavage, range les analyses, rend la sortie assemblée.

    Rend une **chaîne** (la sortie à écrire) ou un **entier** (code de retour 4)
    quand la réponse est inexploitable. Le 4 est propre au contexte : `0`, `1` et
    `3` restent ceux d'`ai_query`, et un appelant shell doit pouvoir distinguer
    « le modèle a répondu n'importe quoi » d'un échec de transport.

    Un appariement partiel est refusé comme un JSON invalide : classer une
    analyse sous le mauvais document empoisonnerait le résumé de session en
    silence, ce qui est pire qu'un échec explicite.
    """
    charge = jsonout.extract_json_object(reponse)
    paires = context_cache.apparier(charge, contexte["a_analyser"]) if charge else None
    if paires is None:
        motif = (
            "aucun objet JSON lisible dans la réponse"
            if charge is None
            else "les documents de la réponse ne correspondent pas à ceux envoyés"
        )
        sys.stderr.write(f"Erreur : {motif}.\n")
        meta["error_type"] = "invalid_json"
        meta["error_detail"] = motif[:200]
        meta["cost_usd"] = cout_total
        meta["documents"] = len(contexte["documents"])
        meta["paved"] = contexte["paves"]
        quota.append_ledger(dict(ligne, status="error", error_type="invalid_json"))
        quota.save_state(quota_state)
        health.save(health_state)
        ai_query.write_receipt(mode, "error", **meta)
        return 4

    if args.no_cache:
        # `--no-cache` veut dire « ne me sers pas du cache » : mémoriser la
        # réponse serait surprenant au tour suivant. On la garde en mémoire pour
        # la sortie, et rien de plus.
        for document, analyse in zip(contexte["a_analyser"], paires):
            document["analyse"] = analyse
    else:
        context_cache.store(
            contexte,
            paires,
            model=ligne.get("model"),
            source=args.source,
            ts=ligne.get("ts"),
        )

    meta["documents"] = len(contexte["documents"])
    meta["paved"] = contexte["paves"]
    meta["cached_documents"] = contexte["paves"]
    return context_cache.assembler(contexte["documents"])


def _sortie_recherche(
    resultat: dict, *, disambigue: bool, choix: str = "", motif: str = ""
) -> dict:
    """Charge de sortie du mode `search` — même forme dans les deux branches.

    `path` sort toujours **tel que le parcours local l'a écrit** : dans la branche
    locale c'est la tête du classement, dans la branche désambiguïsée le candidat
    retenu. Un appelant peut donc rouvrir le chemin sans le revalider auprès de
    qui que ce soit.
    """
    hits = list(resultat.get("hits") or [])
    sortie = {
        "query": resultat.get("query", ""),
        "roots": resultat.get("roots", []),
        "scanned": resultat.get("scanned", 0),
        "truncated": resultat.get("truncated", False),
        "disambiguated": disambigue,
        "path": None,
        "line": None,
        "text": None,
        "hits": hits,
    }
    if disambigue:
        retenu = next((hit for hit in hits if hit["path"] == choix), None)
        sortie["reason"] = motif
    else:
        retenu = hits[0] if hits else None
    if retenu is not None:
        sortie["path"] = retenu["path"]
        sortie["line"] = retenu.get("line")
        sortie["text"] = retenu.get("text")
    return sortie


def _servir_recherche_locale(
    args, resultat: dict, mode: str, tier: str, etat_quota: dict
) -> int:
    """Sortie rendue par le disque seul : aucune requête n'est partie.

    C'est le cas nominal du mode `search` — un nom de fichier se retrouve sans
    l'aide de personne. Le journal d'usage compte les **appels partis** : y écrire
    une ligne pour une réponse purement locale fausserait la seule mesure qui dit
    combien de requêtes coûte la délégation, exactement comme pour le cache de
    contexte.
    """
    sortie = _sortie_recherche(resultat, disambigue=False)
    texte = json.dumps(sortie, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as out_f:
            out_f.write(texte + "\n")
        sys.stderr.write(
            f"[ai_broker] Écrit dans {args.output} (recherche locale, 0 appel)\n"
        )
    else:
        print(texte)
    ai_query.write_receipt(
        mode,
        "ok",
        output=args.output,
        tier=tier,
        local=True,
        calls=0,
        query=resultat["query"],
        hits=len(resultat["hits"]),
        path=sortie["path"],
        disambiguated=False,
        scanned=resultat["scanned"],
        truncated=resultat["truncated"],
        prompt_chars=0,
        cost_usd=0.0,
        quota=etat_quota,
        source=args.source,
        session=os.environ.get("CLAUDE_SESSION_ID"),
    )
    return 0


def _finaliser_recherche(
    reponse: str,
    recherche: dict,
    *,
    ligne: dict,
    meta: dict,
    cout_total: float,
    mode: str,
    quota_state: dict,
    health_state: dict,
):
    """Valide le choix du modèle et rend la sortie, ou le code 4.

    Le modèle ne choisit pas librement : il désigne un chemin **déjà présent dans
    la liste qu'on lui a fermée**. Un chemin inventé, un chemin hors liste et une
    réponse sans objet JSON sont le même échec, et il porte le code 4 du mode
    `context` — un appelant shell doit pouvoir distinguer « le modèle a répondu à
    côté » d'une panne de transport.

    Le chemin retenu repart **tel qu'il figurait dans la liste**, pas dans la
    graphie du modèle : c'est celui qu'un appelant peut rouvrir.
    """
    candidats = locate.ambigue(recherche) or []
    chemins = [hit["path"] for hit in candidats]
    charge = jsonout.extract_json_object(reponse)
    choix = ""
    motif = ""
    if isinstance(charge, dict):
        choix = str(charge.get("path") or "").strip()
        motif = str(charge.get("reason") or "").strip()[:300]

    retenu = None
    if choix:
        retenu = next(
            (
                chemin
                for chemin in chemins
                if os.path.normpath(chemin) == os.path.normpath(choix)
            ),
            None,
        )
    if retenu is None:
        detail = (
            "aucun objet JSON lisible dans la réponse"
            if charge is None
            else f"choix hors de la liste des candidats : {choix!r}"
        )
        sys.stderr.write(f"Erreur : {detail}.\n")
        meta["error_type"] = "invalid_json"
        meta["error_detail"] = detail[:200]
        meta["cost_usd"] = cout_total
        meta["hits"] = len(recherche["hits"])
        meta["candidates"] = len(candidats)
        quota.append_ledger(dict(ligne, status="error", error_type="invalid_json"))
        quota.save_state(quota_state)
        health.save(health_state)
        ai_query.write_receipt(mode, "error", **meta)
        return 4

    meta["path"] = retenu
    meta["reason"] = motif
    meta["hits"] = len(recherche["hits"])
    meta["candidates"] = len(candidats)
    meta["disambiguated"] = True
    meta["cost_usd"] = cout_total
    return json.dumps(
        _sortie_recherche(recherche, disambigue=True, choix=retenu, motif=motif),
        ensure_ascii=False,
        indent=2,
    )


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Passerelle multi-fournisseurs (gratuit d'abord) pour Radar."
    )
    parser.add_argument("prompt", nargs="?", default="", help="Instruction ou prompt.")
    parser.add_argument(
        "-t", "--tier",
        choices=sorted(ai_query.TIER_CASCADES),
        help="Force le profil : 'fast' (logs/diffs/docs) ou 'heavy' (code/tests). "
             "Par défaut déduit du --mode.",
    )
    parser.add_argument(
        "--mode",
        choices=prompts.mode_choices(),
        default="general",
        help="Rôle spécialisé : code, test, context, search, diag, summary, pr, "
             "reasoning, ou general. `read` reste accepté, mais `context` le "
             "remplace (cache disque et pavage multi-documents).",
    )
    parser.add_argument(
        "-f", "--file",
        action="append",
        default=[],
        help="Fichier(s) à injecter en contexte (option répétable).",
    )
    parser.add_argument(
        "-s", "--system",
        default="",
        help="Instruction système sur mesure (remplace celle du --mode).",
    )
    parser.add_argument(
        "-m", "--model",
        help="Force un modèle précis (court-circuite le routeur).",
    )
    parser.add_argument(
        "--raw-code",
        action="store_true",
        help="Ne garde que le contenu des blocs de code (implicite en --mode code/test).",
    )
    parser.add_argument(
        "--check-syntax",
        action="store_true",
        help="Refuse une sortie qui ne compile pas en Python (code de retour 3).",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=1,
        dest="max_repairs",
        help="Tours de relance autorisés quand le code généré ne compile pas, en "
             "mode code/test (défaut : 1, 0 pour refuser du premier coup).",
    )
    parser.add_argument(
        "--no-envelope",
        action="store_false",
        dest="envelope",
        help="Désactive l'enveloppe JSON des modes code/test : la réponse est "
             "écrite telle quelle, sans vérification de syntaxe ni relance.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        dest="no_cache",
        help="Mode context : ignore l'index de cache et renvoie tous les "
             "documents à l'analyse, sans rien mémoriser. À utiliser quand on se "
             "méfie d'une entrée de cache.",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        dest="roots",
        help="Mode search : racine à parcourir (option répétable ; défaut : le "
             "répertoire courant).",
    )
    parser.add_argument(
        "--max-hits",
        type=int,
        default=locate.MAX_HITS_DEFAUT,
        dest="max_hits",
        help="Mode search : nombre maximal de correspondances conservées "
             f"(défaut : {locate.MAX_HITS_DEFAUT}).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Lit l'entrée standard (logs, diffs, etc.).",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        dest="json_mode",
        help="Force une sortie au format JSON structuré (implicite en --mode "
             "context/search).",
    )
    parser.add_argument(
        "-o", "--output",
        help="Écrit la réponse dans un fichier au lieu de stdout.",
    )
    # --- Leviers propres à la passerelle multi-fournisseurs ---
    parser.add_argument(
        "--allow-paid",
        action="store_true",
        help="Autorise explicitement un appel payant pour cette invocation. "
             "Reste soumis au plafond journalier.",
    )
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="Signale une escalade consécutive à un échec vérifié (compilation, "
             "JSON invalide, test au rouge). Trace la raison dans le reçu.",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
        help="Budget de réflexion, traduit dans le champ propre au fournisseur.",
    )
    parser.add_argument(
        "--provider",
        help="Restreint le routage à un seul fournisseur du catalogue. Le filtre "
             "s'applique avant le plafond `--max-candidates` : un fournisseur dont "
             "les modèles sont mal classés reste joignable.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        help="Nombre maximal de candidats essayés (défaut : celui du catalogue).",
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        dest="list_candidates",
        help="Affiche le routage — retenus et écartés, avec le motif de chaque "
             "écart — puis s'arrête sans appeler personne ni écrire de reçu. "
             "Montre tout le catalogue : `--provider` ne s'y applique pas.",
    )
    parser.add_argument(
        "--source",
        default=RECEIPT_SOURCE_DEFAULT,
        help="Étiquette d'origine de l'appel, recopiée dans le reçu (distingue "
             "la production d'un banc ou d'un test).",
    )
    parser.add_argument(
        "--catalogue",
        help="Chemin du catalogue de modèles (défaut : config/ai_models.json).",
    )
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        dest="no_sleep",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    catalogue = cat_mod.load(args.catalogue)
    policy = cat_mod.policy(catalogue)

    mode = args.mode
    if mode == "read":
        sys.stderr.write(
            "[ai_broker] --mode read est remplacé par --mode context (cache disque "
            "et pavage multi-documents) ; `read` reste accepté pour l'instant.\n"
        )
    tier = prompts.resolve_mode_tier(mode, args.tier)
    system_instruction = args.system or prompts.system_prompt(mode)

    # L'enveloppe ne concerne que les modes qui rendent du code : ailleurs, elle
    # transformerait une prose attendue (diagnostic, revue) en objet JSON que
    # l'appelant ne sait pas lire.
    enveloppe = bool(args.envelope) and mode in prompts.CODE_LIKE_MODES
    # L'enveloppe implique la vérification de syntaxe : c'est tout son intérêt.
    # `--check-syntax` seul reste honoré, pour les appels qui passent du code par
    # un mode générique.
    verifier_syntaxe = bool(args.check_syntax) or enveloppe
    # `max_reparations` est calculé plus bas, avec le contrat de sortie : il en
    # dépend, et le contrat de `context` et `search` n'est connu qu'après la
    # préparation du pavage et du parcours local.

    quota_state = quota.load_state()
    health_state = health.load()

    # --- Routage : instruit même quand aucun appel ne suit ---
    if args.list_candidates:
        infos = router.explain(
            catalogue,
            mode=mode,
            tier=tier,
            allow_paid=args.allow_paid,
            escalate=args.escalate,
        )
        if not infos:
            sys.stderr.write(
                "Aucun modèle au catalogue : lancez scripts/ai/refresh_models.py.\n"
            )
            return 1
        print(router.format_explain(infos))
        return 0

    # --- Mode search : le disque d'abord, le modèle seulement pour départager ---
    # Placé après le court-circuit de `--list-candidates`, pour la même raison que
    # le mode context : montrer le routage ne doit parcourir aucun fichier.
    #
    # Le cas nominal ne coûte **aucun appel** : tant qu'un seul fichier tient le
    # premier rang, la réponse est rendue telle quelle. Un appel n'a lieu que si
    # deux fichiers distincts se disputent ce rang ; le prompt envoyé n'est alors
    # plus la requête mais la question fermée du départage, candidats compris.
    recherche = None
    if mode == "search":
        resultat = locate.chercher(
            args.prompt,
            roots=args.roots,
            max_hits=args.max_hits,
        )
        if not resultat["hits"]:
            motif = (
                f"aucun fichier ne correspond à {resultat['query']!r} "
                f"({resultat['scanned']} fichier(s) parcouru(s))"
            )
            sys.stderr.write(f"Erreur : {motif}.\n")
            # Rien n'est parti, mais l'échec est réel : le reçu le dit, sans ligne
            # de journal — le journal compte les appels, et il n'y en a pas eu.
            ai_query.write_receipt(
                mode,
                "error",
                error_type="no_match",
                error_detail=motif[:200],
                tier=tier,
                query=resultat["query"],
                roots=resultat["roots"],
                scanned=resultat["scanned"],
                truncated=resultat["truncated"],
                hits=0,
                calls=0,
                prompt_chars=0,
                cost_usd=0.0,
                quota=_etat_quota(catalogue, quota_state, policy),
                session=os.environ.get("CLAUDE_SESSION_ID"),
                source=args.source,
            )
            return 1
        candidats_recherche = locate.ambigue(resultat)
        if candidats_recherche is None:
            return _servir_recherche_locale(
                args, resultat, mode, tier, _etat_quota(catalogue, quota_state, policy)
            )
        args.prompt = locate.prompt_desambiguisation(
            resultat, candidats_recherche, requete=args.prompt
        )
        recherche = resultat

    # --- Mode context : le cache d'abord, le pavage ensuite ---
    # Placé après le court-circuit de `--list-candidates` : celui-ci doit rester
    # capable de montrer le routage sans lire un seul fichier.
    #
    # Sans fichier, il n'y a rien à mettre en cache ni à assembler : le mode se
    # comporte alors comme un appel JSON ordinaire, et `contexte` reste `None`.
    contexte = None
    if mode == "context" and args.file:
        try:
            contexte = context_cache.prepare(args.file, rafraichir=args.no_cache)
        except FileNotFoundError as exc:
            sys.stderr.write(f"Erreur : fichier introuvable '{exc.args[0]}'\n")
            return 1
        if not contexte["a_analyser"]:
            return _servir_contexte_du_cache(
                args, contexte, mode, tier, _etat_quota(catalogue, quota_state, policy)
            )
        # Le prompt n'est construit qu'avec ce qui n'est pas déjà analysé : c'est
        # le pavage. Les documents déjà connus ne repartent pas au modèle, et la
        # clé de cache qui a servi à les reconnaître est celle du texte numéroté
        # que `_build_prompt` enverra — même fonction, donc même empreinte.
        args.file = [doc["path"] for doc in contexte["a_analyser"]]

    # Le contrat de sortie : ce qui doit revenir, et ce qui sera relu. `contexte`
    # et `recherche` sont déjà connus — ce sont eux qui disent quel schéma
    # s'applique, avant même que le prompt soit construit.
    contrat = _contrat_json(args, contexte, recherche, enveloppe)
    schema_json = contrat[1] if contrat else None
    schema_nom = contrat[0] if contrat else "reponse"
    # L'enveloppe n'est **pas** validée en JSON localement : elle a un repli markdown
    # assumé (voir `envelope.split_answer`), donc exiger son JSON transformerait un
    # repli utilisable en échec. Sa garantie réelle est la syntaxe du code, plus bas.
    valider_json = (
        contexte is not None
        or recherche is not None
        or (contrat is not None and contrat[1] is None)
    )
    # `--max-repairs 0` refuse la relance, y compris pour un contrat JSON : c'est le
    # seul moyen de demander « un premier jet et rien d'autre », et le banc s'en sert.
    max_reparations = max(0, int(args.max_repairs)) if (enveloppe or valider_json) else 0

    full_prompt = _build_prompt(args)
    if not full_prompt:
        sys.stderr.write("Erreur : aucun texte fourni en entrée (prompt, fichier ou stdin).\n")
        return 1
    if enveloppe:
        # Placée en fin de prompt : une consigne de format relue juste avant la
        # génération est mieux suivie que la même enfouie dans le système.
        full_prompt = f"{full_prompt}\n\n{envelope.ENVELOPE_INSTRUCTION}"

    candidats = router.order_candidates(
        catalogue,
        mode=mode,
        tier=tier,
        prompt_tokens=max(1, len(full_prompt) // 4),
        explicit_model=args.model,
        # Filtre passé au routeur, qui l'applique **avant** `max_candidates`.
        # Filtrer ici, après coup, revenait à tronquer d'abord : `--provider X`
        # pouvait rendre « aucun candidat utilisable » quand X n'était pas dans
        # les `max_candidates` premiers candidats gratuits (défaut du 26/09).
        provider=args.provider,
        allow_paid=args.allow_paid,
        escalate=args.escalate,
        quota_state=quota_state,
        health_state=health_state,
        max_candidates=args.max_candidates,
    )
    if not candidats:
        raisons = router.explain(
            catalogue,
            mode=mode,
            tier=tier,
            allow_paid=args.allow_paid,
            escalate=args.escalate,
            quota_state=quota_state,
            health_state=health_state,
        )
        sys.stderr.write("Erreur : aucun candidat utilisable.\n")
        if args.provider:
            sys.stderr.write(
                f"    (filtre fournisseur « {args.provider} » : aucun de ses "
                "candidats n'est proposable ; la table ci-dessous montre l'état "
                "de tout le catalogue)\n"
            )
        detail = router.format_explain(raisons)
        if detail:
            sys.stderr.write(detail + "\n")
        ai_query.write_receipt(
            mode,
            "error",
            error_type=ERROR_CASCADE_EXHAUSTED,
            error_detail="aucun candidat utilisable",
            tier=tier,
            prompt_chars=len(full_prompt),
            cost_usd=0.0,
            quota=_etat_quota(catalogue, quota_state, policy),
            session=os.environ.get("CLAUDE_SESSION_ID"),
            source=args.source,
        )
        return 1

    is_json = bool(args.json_mode) or mode in ("read", "context", "search")
    atteindre = _noop if args.no_sleep else _sleep

    started = time.time()
    meta = {
        "tier": tier,
        "prompt_chars": len(full_prompt),
        "session": os.environ.get("CLAUDE_SESSION_ID"),
        "source": args.source,
        "candidates": len(candidats),
    }

    fournisseurs_morts: set[str] = set()
    echecs: list[dict] = []
    tentatives = 0
    dernier_statut: int | None = None
    dernier_motif = ""
    # Dernier candidat réellement tenté : c'est lui qui nomme le reçu d'échec
    # total, sans quoi une cascade épuisée ne dit ni fournisseur ni modèle.
    dernier_candidat: router.Candidate | None = None

    for rang, candidat in enumerate(candidats):
        if candidat.provider in fournisseurs_morts:
            continue

        dernier_candidat = candidat
        q_spec = cat_mod.quota_spec(catalogue, candidat.provider)
        key_id = candidat.key_env or "sans-cle"
        retry = policy.get("retry") or {}
        try:
            essais_max = max(1, int(retry.get("per_model") or 2))
        except (TypeError, ValueError):
            essais_max = 2

        for essai in range(essais_max):
            valeur_cle = _resolve_key(candidat, providers.env_value)
            if candidat.key_env and not valeur_cle:
                dernier_motif = f"clé '{candidat.key_env}' absente au moment de l'appel"
                echecs.append({"candidate": candidat.key, "error": dernier_motif})
                break

            tentatives += 1
            debut_essai = time.time()
            # Compté AVANT l'envoi : une requête partie consomme un quota, même
            # si la réponse se perd. Sous-compter un quota coûte un appel refusé
            # par le fournisseur ; le surestimer ne coûte rien.
            quota.register_call(
                quota_state,
                candidat.provider,
                key_id,
                q_spec["window"],
                now=time.time(),
                reset_window=None,
            )

            try:
                completion = _appeler(
                    candidat,
                    full_prompt,
                    system_instruction=system_instruction,
                    is_json=is_json,
                    effort=args.effort,
                    valeur_cle=valeur_cle,
                    # Le contrat voyage dès le premier jet : quand le fournisseur
                    # sait le traduire, le JSON est natif et la relecture qui suit
                    # n'est qu'un filet. Quand il ne sait pas, elle est la garantie.
                    json_schema=schema_json,
                    schema_name=schema_nom,
                )
            except providers.ProviderError as exc:
                statut, motif = _enregistrer_echec_appel(
                    exc,
                    candidat=candidat,
                    key_id=key_id,
                    health_state=health_state,
                    quota_state=quota_state,
                    mode=mode,
                    tier=tier,
                    source=args.source,
                    ts=debut_essai,
                    attempt_index=essai + 1,
                    fournisseurs_morts=fournisseurs_morts,
                )
                dernier_statut = statut
                dernier_motif = motif
                echecs.append(
                    {"candidate": candidat.key, "error": motif, "status": statut}
                )
                if getattr(exc, "retryable", False) and essai + 1 < essais_max:
                    atteindre(_retry_delay(essai + 1, policy))
                    continue
                break
            except Exception as exc:  # pragma: no cover - filet de sécurité
                dernier_motif = str(exc)
                health.record_failure(
                    health_state, candidat.provider, candidat.model, detail=str(exc)
                )
                echecs.append({"candidate": candidat.key, "error": str(exc)})
                break

            texte = completion.text or ""
            if not texte.strip():
                # Réponse vide : ni un succès, ni une erreur du fournisseur. Elle
                # ne doit pas être écrite dans un fichier, donc elle échoue ici.
                dernier_motif = f"réponse vide de {candidat.key}"
                health.record_failure(
                    health_state,
                    candidat.provider,
                    candidat.model,
                    detail=dernier_motif,
                )
                echecs.append({"candidate": candidat.key, "error": dernier_motif})
                quota.append_ledger(
                    {
                        "ts": debut_essai,
                        "provider": candidat.provider,
                        "model": candidat.model,
                        "key_id": key_id,
                        "mode": mode,
                        "tier": tier,
                        "free": candidat.free,
                        "paid": not candidat.free,
                        "status": "error",
                        "error_type": "empty_response",
                        "attempt_index": essai + 1,
                        "elapsed_ms": round((time.time() - debut_essai) * 1000),
                        "source": args.source,
                    }
                )
                if essai + 1 < essais_max:
                    atteindre(_retry_delay(essai + 1, policy))
                    continue
                break

            # --- Succès du fournisseur : la réponse reste à valider ---
            cout = _charge_paid(completion, candidat, quota_state, time.time())
            # Cumul de la chaîne (premier jet + réparations) : c'est cette somme
            # qui est réellement imputée au plafond du jour, et le reçu la porte
            # entière. La ligne de journal, elle, garde le coût de son appel.
            cout_total = cout
            health.record_success(health_state, candidat.provider, candidat.model)
            meta.update(completion.usage or {})
            # Jetons dans la même forme que la ligne de journal (`tokens` imbriqué),
            # en plus des champs à plat de `usage` : le tableau de bord lit un reçu
            # avec la même grille qu'une ligne d'usage.
            meta["tokens"] = dict(completion.usage or {})
            meta.update(
                model=completion.model,
                provider=candidat.provider,
                paid=not candidat.free,
                cost_usd=cout,
                key_id=key_id,
                attempts=tentatives,
                fallbacks=rang,
                redactions=sum((completion.redactions or {}).values()),
                routing_reason=candidat.reason,
                elapsed_ms=round((time.time() - started) * 1000),
                # Le quota restant de la clé gagnante, lu après l'appel : c'est ce
                # chiffre qui dit au tableau de bord si le gratuit s'épuise.
                quota=_etat_quota(
                    catalogue,
                    quota_state,
                    policy,
                    provider=candidat.provider,
                    key_id=key_id,
                ),
            )

            reponse, explication = _extraire_sortie(
                texte, enveloppe=enveloppe, raw_code=args.raw_code, mode=mode
            )

            # La ligne de journal n'est écrite qu'une fois la sortie validée.
            # Elle était écrite avant l'extraction du code : une réponse vide y
            # laissait un `status: "ok"` alors que le reçu, lui, disait `error`.
            # Le tableau de bord et la boucle de mesure lisent ce journal pour
            # calculer un taux de succès — un « ok » pour un appel que le
            # courtier refuse fausse la seule mesure qui compte.
            ligne = {
                "ts": debut_essai,
                "provider": candidat.provider,
                "model": candidat.model,
                "key_id": key_id,
                "mode": mode,
                "tier": tier,
                "free": candidat.free,
                "paid": not candidat.free,
                "cost_usd": cout,
                "attempt_index": essai + 1,
                "tokens": completion.usage or {},
                "elapsed_ms": round((time.time() - debut_essai) * 1000),
                "source": args.source,
            }

            # --- Validation de la sortie, et réparation si elle est cassée ---
            # Une seule boucle pour trois besoins : réponse vide, code qui ne
            # compile pas, relance. `reparations` compte les tours déjà joués
            # (0 = premier jet) : c'est aussi le `repair_round` du journal, donc
            # le nombre de tours se relit dans le fichier d'usage au lieu de se
            # déduire du reçu.
            reparations = 0
            abandonner = False
            while True:
                if not reponse.strip():
                    motif_vide = f"réponse vide de {candidat.key}"
                    if reparations:
                        motif_vide += f" au tour de réparation {reparations}"
                    sys.stderr.write(f"Erreur : {motif_vide}, rien à écrire.\n")
                    dernier_motif = motif_vide
                    echecs.append({"candidate": candidat.key, "error": motif_vide})
                    meta["error_type"] = "empty_response"
                    meta["repairs"] = reparations
                    meta["cost_usd"] = cout_total
                    # L'appel est parti : sa trace et son quota doivent être
                    # persistés même en sortie d'erreur. Ne pas sauver ici
                    # remettrait le compteur à son état d'avant l'appel, et le
                    # fournisseur refuserait le suivant.
                    quota.append_ledger(
                        dict(ligne, status="error", error_type="empty_response")
                    )
                    quota.save_state(quota_state)
                    health.save(health_state)
                    ai_query.write_receipt(mode, "error", **meta)
                    return 1

                # Un seul contrôle pour les trois garanties : le code compile, le
                # JSON est lisible, et il dit ce que la demande attendait. Le
                # contrat est désactivé quand la sortie n'a pas de JSON à valider —
                # l'enveloppe des modes de code, dont le repli markdown est assumé,
                # garde la syntaxe pour seule garantie.
                probleme = _probleme_sortie(
                    reponse,
                    verifier_syntaxe=verifier_syntaxe,
                    contrat=contrat if valider_json else None,
                    contexte=contexte,
                    recherche=recherche,
                    demande=full_prompt,
                )
                if probleme is None:
                    break

                # La ligne d'erreur décrit *cette* sortie-là, avec le rang de la
                # relance qui l'a produite : un code cassé au premier jet et un
                # code cassé après réparation ne sont pas le même échec — pas plus
                # qu'un JSON illisible et un chemin choisi hors de la liste fermée.
                quota.append_ledger(
                    dict(
                        ligne,
                        status="error",
                        error_type=probleme["type"],
                        error_detail=probleme["detail"][:200],
                        repair_round=reparations,
                    )
                )
                if reparations >= max_reparations:
                    sys.stderr.write(
                        f"Erreur : sortie inexploitable de {candidat.key} après "
                        f"{reparations} réparation(s) — {probleme['detail']}\n"
                    )
                    meta["error_type"] = probleme["type"]
                    meta["error_detail"] = probleme["detail"][:200]
                    meta["repairs"] = reparations
                    meta["cost_usd"] = cout_total
                    meta["explanation_chars"] = len(explication)
                    # Le reçu porte ce que le mode a réellement envoyé et reçu, avec
                    # les mêmes compteurs que la branche `ok` : un échec JSON doit se
                    # relire avec la grille d'un succès, sinon les deux ne se
                    # comparent pas dans le tableau de bord.
                    if contexte is not None:
                        meta["documents"] = len(contexte["documents"])
                        meta["paved"] = contexte["paves"]
                    if recherche is not None:
                        meta["hits"] = len(recherche["hits"])
                        meta["candidates"] = len(locate.ambigue(recherche) or [])
                    # Même raison que pour la réponse vide : l'appel a bien été
                    # fait, donc il compte dans le quota du jour et laisse une
                    # ligne d'erreur — jamais une ligne « ok ».
                    quota.save_state(quota_state)
                    health.save(health_state)
                    ai_query.write_receipt(mode, "error", **meta)
                    return probleme["code"]

                reparations += 1
                sys.stderr.write(
                    f"[ai_broker] sortie invalide de {candidat.key} "
                    f"(réparation {reparations}/{max_reparations}) : "
                    f"{probleme['detail']}\n"
                )
                debut_reparation = time.time()
                try:
                    # Même comptabilité que le premier jet : une relance est un
                    # appel réel, elle consomme du quota et laisse sa trace.
                    quota.register_call(
                        quota_state,
                        candidat.provider,
                        key_id,
                        q_spec["window"],
                        now=debut_reparation,
                        reset_window=None,
                    )
                    tentatives += 1
                    completion = _appeler(
                        candidat,
                        probleme["prompt"],
                        system_instruction=system_instruction,
                        is_json=is_json,
                        effort=args.effort,
                        valeur_cle=valeur_cle,
                        # La relance repart avec le même contrat : la garantir au
                        # premier jet et la retirer à la relance n'aurait aucun sens.
                        json_schema=schema_json,
                        schema_name=schema_nom,
                    )
                except providers.ProviderError as exc:
                    statut, motif = _enregistrer_echec_appel(
                        exc,
                        candidat=candidat,
                        key_id=key_id,
                        health_state=health_state,
                        quota_state=quota_state,
                        mode=mode,
                        tier=tier,
                        source=args.source,
                        ts=debut_reparation,
                        attempt_index=essai + 1,
                        fournisseurs_morts=fournisseurs_morts,
                        repair_round=reparations,
                    )
                    dernier_statut = statut
                    dernier_motif = motif
                    echecs.append(
                        {"candidate": candidat.key, "error": motif, "status": statut}
                    )
                    abandonner = True
                    break
                except Exception as exc:  # pragma: no cover - filet de sécurité
                    dernier_motif = str(exc)
                    health.record_failure(
                        health_state, candidat.provider, candidat.model, detail=str(exc)
                    )
                    echecs.append({"candidate": candidat.key, "error": str(exc)})
                    abandonner = True
                    break

                texte = completion.text or ""
                # La relance se relit avec la même règle que le premier jet, sinon
                # une réparation de mode `context` serait découpée comme une
                # enveloppe de code — et rendrait une réponse vide, donc un échec.
                reponse, explication = _extraire_sortie(
                    texte, enveloppe=enveloppe, raw_code=args.raw_code, mode=mode
                )
                cout = _charge_paid(completion, candidat, quota_state, time.time())
                cout_total += cout
                meta.update(completion.usage or {})
                meta["tokens"] = dict(completion.usage or {})
                ligne.update(
                    ts=debut_reparation,
                    model=completion.model,
                    cost_usd=cout,
                    tokens=completion.usage or {},
                    elapsed_ms=round((time.time() - debut_reparation) * 1000),
                )

            if abandonner:
                # La relance n'a pas abouti : c'est un échec d'appel, pas un échec
                # de syntaxe. Le candidat suivant prend la main, sans écrire de
                # ligne supplémentaire — celle du refus vient d'être écrite.
                break

            meta["repairs"] = reparations
            meta["cost_usd"] = cout_total
            meta["explanation_chars"] = len(explication)
            if explication:
                # L'explication part sur stderr : stdout reste du code pur, donc
                # redirigeable dans un fichier sans nettoyage. Bornée pour qu'un
                # modèle bavard ne noie pas la console de l'appelant.
                sys.stderr.write(f"[ai_broker] explication : {explication[:2000]}\n")

            if contexte is not None:
                # Le JSON du pavage est validé *avant* toute écriture : un objet à
                # moitié valide ne doit jamais atterrir sur disque, ni dans la
                # sortie, ni dans l'index de cache.
                final = _finaliser_contexte(
                    reponse,
                    contexte,
                    ligne=ligne,
                    meta=meta,
                    cout_total=cout_total,
                    mode=mode,
                    args=args,
                    quota_state=quota_state,
                    health_state=health_state,
                )
                if isinstance(final, int):
                    return final
                reponse = final

            if recherche is not None:
                # Le modèle n'a eu qu'à désigner un chemin dans une liste fermée :
                # un chemin hors liste est un échec explicite (code 4), jamais un
                # chemin deviné qu'un appelant suivrait les yeux fermés.
                final = _finaliser_recherche(
                    reponse,
                    recherche,
                    ligne=ligne,
                    meta=meta,
                    cout_total=cout_total,
                    mode=mode,
                    quota_state=quota_state,
                    health_state=health_state,
                )
                if isinstance(final, int):
                    return final
                reponse = final

            quota.append_ledger(dict(ligne, status="ok"))

            if args.output:
                with open(args.output, "w", encoding="utf-8") as out_f:
                    out_f.write(reponse + "\n")
                sys.stderr.write(
                    f"[ai_broker] Écrit dans {args.output} via {candidat.key} "
                    f"({'gratuit' if candidat.free else f'{cout:.6f} $'})\n"
                )
            else:
                print(reponse)

            quota.save_state(quota_state)
            health.save(health_state)
            ai_query.write_receipt(mode, "ok", output=args.output, **meta)
            return 0

    # --- Aucun candidat n'a répondu ---
    quota.save_state(quota_state)
    health.save(health_state)

    if echecs:
        sys.stderr.write("Erreur : aucun fournisseur n'a répondu.\n")
        for echec in echecs:
            sys.stderr.write(f"  - {echec.get('candidate')} : {echec.get('error')}\n")

    meta["attempts"] = tentatives
    meta["fallbacks"] = max(0, tentatives - 1)
    meta["cascade"] = echecs
    # « Qui a refusé » doit se lire dans le reçu, pas seulement dans le journal :
    # sans cela, une cascade épuisée ne se rattache à aucun quota ni disjoncteur.
    if dernier_candidat is not None:
        meta["provider"] = dernier_candidat.provider
        meta["model"] = dernier_candidat.model
        meta["quota"] = _etat_quota(
            catalogue,
            quota_state,
            policy,
            provider=dernier_candidat.provider,
            key_id=dernier_candidat.key_env or "sans-cle",
        )
    else:
        meta["quota"] = _etat_quota(catalogue, quota_state, policy)
    if dernier_statut is not None:
        meta["error_type"] = f"http_{dernier_statut}"
    else:
        meta["error_type"] = ERROR_CASCADE_EXHAUSTED
    meta["error_detail"] = str(dernier_motif or "cascade épuisée")[:200]
    meta["elapsed_ms"] = round((time.time() - started) * 1000)
    ai_query.write_receipt(mode, "error", **meta)
    return 1


if __name__ == "__main__":
    sys.exit(main())
