#!/usr/bin/env python3
"""Orchestrateur CLI de la suite de benchmark LLM (scripts/bench).

Stratégie : l'évaluation déterministe (0 jeton) d'abord, le juge LLM
uniquement quand il apporte de l'information sémantique sur une sortie
qui a déjà passé les validations déterministes.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# La racine du dépôt doit être résolvable AVANT d'importer `scripts.bench.*`,
# pour que `python3 scripts/bench/run.py` fonctionne sans installation du paquet.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.bench import harness, judge, scenarios  # noqa: E402


# Budget borné à 200 000 jetons par passe : le plafond nous force à privilégier
# l'évaluation déterministe, sinon un juge LLM sur chaque (modèle, scénario)
# consommerait la totalité du budget avant la fin des mesures.
DEFAULT_BUDGET_TOKENS = 200_000
DEFAULT_JUDGE_MODEL = "gemini:gemini-3.5-flash-lite"

# En-têtes CSV en français et dans l'ordre imposé par la spécification.
ROW_COLUMNS = (
    "modele",
    "scenario",
    "succes_deterministe",
    "ok",
    "note_juge",
    "latence_s",
    "ttft_s",
    "tokens_in",
    "tokens_out",
    "tok_par_s",
    "tokens_juge",
    "detail",
)


@dataclass
class Row:
    """Une ligne de résultat, prête à être sérialisée en CSV ou agrégée."""

    modele: str
    scenario: str
    succes_deterministe: float
    ok: int
    note_juge: int | None
    latence_s: float
    ttft_s: float | None
    tokens_in: int
    tokens_out: int
    tok_par_s: float | None
    tokens_juge: int
    detail: str


def _short(model: str) -> str:
    """Conserve la partie lisible d'un identifiant `fournisseur:modele`."""
    # au premier « : » seulement : « :free » fait partie du nom OpenRouter
    return model.split(":", 1)[-1] or model


def _flatten_detail(text: str, limit: int = 100) -> str:
    # On neutralise les retours ligne et on borne la longueur : une ligne CSV
    # doit rester sur une seule ligne et ne jamais embarquer de contenu brut.
    flat = " ".join((text or "").split())
    return flat[:limit]


def _emit(lock: threading.Lock, rows: list[Row], row: Row, message: str) -> None:
    """Ajoute une ligne sous verrou et imprime la progression de façon atomique."""
    with lock:
        rows.append(row)
        print(message, flush=True)


def _run_model(
    model: str,
    scens: list[scenarios.Scenario],
    *,
    budget: harness.TokenBudget,
    judge_model: str,
    timeout: float,
    lock: threading.Lock,
    rows: list[Row],
) -> None:
    """Exécute tous les scénarios d'un modèle, en séquence, sous contrainte de budget."""
    short = _short(model)
    for sc in scens:
        if budget.exhausted:
            # Budget épuisé : aucun appel courtier, on marque la ligne pour que
            # le CSV reste homogène et que l'absence de mesure soit traçable.
            row = Row(
                modele=model,
                scenario=sc.id,
                succes_deterministe=0.0,
                ok=0,
                note_juge=None,
                latence_s=0.0,
                ttft_s=None,
                tokens_in=0,
                tokens_out=0,
                tok_par_s=None,
                tokens_juge=0,
                detail="budget",
            )
            _emit(lock, rows, row, f"[{short}] {sc.id} budget=epuise")
            continue

        res = harness.call_broker(
            model,
            sc.build_prompt(),
            mode=sc.mode,
            extra_args=sc.extra_args,
            timeout=timeout,
        )
        # Toute réponse consommée (succès ou échec) est débitée du budget commun.
        budget.add(res.tokens_in + res.tokens_out)

        if res.rc != 0:
            score = 0.0
            ok = 0
            detail = _flatten_detail(res.stderr or "echec courtier")
        else:
            chk = sc.check(res.stdout)
            score = float(chk.score)
            ok = 1 if chk.ok else 0
            detail = _flatten_detail(chk.detail)

        note_juge: int | None = None
        tokens_juge = 0
        # Juge LLM uniquement si le scénario déclare des critères sémantiques et
        # que le déterministe a déjà validé (>=0.75) : noter sémantiquement une
        # sortie objectivement cassée brûlerait des jetons pour rien.
        if sc.judge_criteria is not None and score >= 0.75 and not budget.exhausted:
            jr = judge.judge(sc.judge_criteria, res.stdout, model=judge_model)
            budget.add(jr.tokens)
            tokens_juge = jr.tokens
            note_juge = jr.score
            if jr.error:
                detail = _flatten_detail(f"{detail} | juge: {jr.error}")

        row = Row(
            modele=model,
            scenario=sc.id,
            succes_deterministe=round(score, 2),
            ok=ok,
            note_juge=note_juge,
            latence_s=round(res.latency_s, 3),
            ttft_s=res.ttft_s,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            tok_par_s=res.tok_per_s,
            tokens_juge=tokens_juge,
            detail=detail,
        )
        note_txt = "-" if note_juge is None else str(note_juge)
        _emit(
            lock,
            rows,
            row,
            f"[{short}] {sc.id} score={score:.2f} juge={note_txt} lat={res.latency_s:.1f}s",
        )


def _write_csv(path: str, rows: list[Row]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    # utf-8 explicite et newline='' pour laisser csv gérer les fins de ligne.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ROW_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "modele": row.modele,
                    "scenario": row.scenario,
                    "succes_deterministe": row.succes_deterministe,
                    "ok": row.ok,
                    "note_juge": "" if row.note_juge is None else row.note_juge,
                    "latence_s": row.latence_s,
                    # Le courtier n'expose pas de streaming : le TTFT est vide.
                    "ttft_s": "" if row.ttft_s is None else round(row.ttft_s, 3),
                    "tokens_in": row.tokens_in,
                    "tokens_out": row.tokens_out,
                    "tok_par_s": "" if row.tok_par_s is None else round(row.tok_par_s, 2),
                    "tokens_juge": row.tokens_juge,
                    "detail": row.detail,
                }
            )


def _print_summary(rows: list[Row], budget: harness.TokenBudget) -> None:
    print()
    print("=== Recapitulatif par modele ===")

    agg: dict[str, dict] = {}
    for row in rows:
        d = agg.setdefault(
            row.modele,
            {"n": 0, "score_sum": 0.0, "judge": [], "lat_sum": 0.0, "tokens": 0},
        )
        d["n"] += 1
        d["score_sum"] += row.succes_deterministe
        if row.note_juge is not None:
            d["judge"].append(row.note_juge)
        d["lat_sum"] += row.latence_s
        d["tokens"] += row.tokens_in + row.tokens_out + row.tokens_juge

    # Tri : d'abord meilleure réussite déterministe, puis latence croissante.
    order = sorted(
        agg.items(),
        key=lambda kv: (
            -(kv[1]["score_sum"] / kv[1]["n"]) if kv[1]["n"] else 0.0,
            (kv[1]["lat_sum"] / kv[1]["n"]) if kv[1]["n"] else 0.0,
        ),
    )

    header = "{:<32} {:>9} {:>9} {:>11} {:>12}".format(
        "modele", "succes%", "juge moy", "lat moy", "jetons"
    )
    print(header)
    print("-" * len(header))
    for name, d in order:
        n = d["n"] or 1
        succ = 100.0 * d["score_sum"] / n
        jmoy = (
            "{:.1f}".format(sum(d["judge"]) / len(d["judge"]))
            if d["judge"]
            else "-"
        )
        lat = d["lat_sum"] / n
        print(
            "{:<32} {:>8.1f}% {:>9} {:>10.1f}s {:>12}".format(
                _short(name), succ, jmoy, lat, d["tokens"]
            )
        )

    # Consommation réelle du budget : limit - remaining compte aussi les jetons
    # dépensés par le juge, que le tableau par modèle additionne également.
    consommes = budget.limit - budget.remaining
    pct = (100.0 * consommes / budget.limit) if budget.limit else 0.0
    print()
    print(
        "Jetons consommes : {} / {} ({:.1f}%) | reste {} | budget epuise: {}".format(
            consommes,
            budget.limit,
            pct,
            budget.remaining,
            "oui" if budget.exhausted else "non",
        )
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Suite de benchmark LLM via le courtier scripts/ai_broker.py.",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Liste de modeles separes par des virgules (defaut : modeles libres).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ne garder que les N premiers modeles.",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help="Ids de scenarios separes par des virgules (defaut : tous).",
    )
    parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        help="Modele du juge LLM (defaut : %(default)s).",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET_TOKENS,
        help="Budget total de jetons pour la passe (defaut : %(default)s).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Nombre de modeles evalues en parallele (defaut : %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Timeout en secondes par appel courtier (defaut : %(default)s).",
    )
    parser.add_argument(
        "--out",
        default="./bench_suite.csv",
        help="Chemin du CSV de sortie (defaut : %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = list(harness.list_free_models())
    if args.limit is not None:
        models = models[: max(args.limit, 0)]

    if args.scenarios:
        scenario_ids = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    else:
        scenario_ids = None
    scens = scenarios.get_scenarios(scenario_ids)

    if not models:
        print("Aucun modele a evaluer.", file=sys.stderr)
        return 2
    if not scens:
        print("Aucun scenario a evaluer.", file=sys.stderr)
        return 2

    budget = harness.TokenBudget(args.budget)
    rows: list[Row] = []
    lock = threading.Lock()
    workers = max(1, args.workers)

    # Un modele par tache, scenarios sequentiels a l'interieur : on isole les
    # mesures de latence par modele et on evite de saturer le courtier.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_model,
                model,
                scens,
                budget=budget,
                judge_model=args.judge_model,
                timeout=args.timeout,
                lock=lock,
                rows=rows,
            ): model
            for model in models
        }
        for fut in as_completed(futures):
            model = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # pragma: no cover - garde-fou d'orchestration
                print(f"[{_short(model)}] erreur inattendue : {exc}", file=sys.stderr)

    _write_csv(args.out, rows)
    _print_summary(rows, budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
