import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List

from scripts.bench import harness, deep

# Verrou global pour l'écriture sécurisée dans le fichier de résultats JSONL
file_lock = threading.Lock()

def get_completed_ids(results_path: Path) -> set:
    """Lit le fichier results.jsonl existant pour permettre la reprise en évitant les doublons."""
    completed = set()
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "id" in data:
                        completed.add(data["id"])
                except json.JSONDecodeError:
                    continue
    return completed

def execute_item(item: Dict[str, Any], budget: harness.TokenBudget, model_locks: Dict[str, threading.Lock]) -> Dict[str, Any]:
    """Exécute une seule épreuve en respectant le verrou par modèle et le budget."""
    item_id = item["id"]
    model = item["model"]
    niveau = item["niveau"]
    effort = item.get("effort")
    tier = item.get("tier")
    timeout = item.get("timeout") or 120

    # Vérification du budget global
    if budget.exhausted:
        return {
            "id": item_id,
            "model": model,
            "epreuve": item["epreuve"],
            "niveau": niveau,
            "effort": effort,
            "tier": tier,
            "rc": -1,
            "latence_s": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tok_per_s": 0.0,
            "conformite": 0.0,
            "execution": 0.0,
            "detail": "Budget épuisé",
            "sortie": "",
            "erreur": "budget"
        }

    try:
        ep = deep.get_epreuve(item["epreuve"])
    except Exception as e:
        return {
            "id": item_id,
            "model": model,
            "epreuve": item["epreuve"],
            "niveau": niveau,
            "effort": effort,
            "tier": tier,
            "rc": -1,
            "latence_s": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tok_per_s": 0.0,
            "conformite": 0.0,
            "execution": 0.0,
            "detail": f"Erreur chargement épreuve: {e}",
            "sortie": "",
            "erreur": str(e)
        }

    prompt = ep.build_prompt(niveau)
    extra_args = list(ep.base_args)
    if effort:
        extra_args.extend(["--effort", effort])
    if tier:
        extra_args.extend(["-t", tier])

    # Un thread par modèle pour éviter les 429 auto-infligés
    lock = model_locks.setdefault(model, threading.Lock())
    with lock:
        res = harness.call_broker(model, prompt, mode=ep.mode, extra_args=extra_args, timeout=timeout)

    budget.add(res.tokens_in + res.tokens_out)

    if res.rc != 0:
        conformite = 0.0
        execution = 0.0
        detail = "Erreur d'exécution broker"
        sortie = res.stdout[:2500] if res.stdout else ""
        erreur = res.stderr[:200] if res.stderr else "Erreur inconnue"
    else:
        try:
            score_res = ep.score(res.stdout, niveau)
            # DetScore est un dataclass : .get() levait AttributeError et toutes les notes tombaient à 0
            conformite = float(score_res.conformite)
            execution = float(score_res.execution)
            detail = score_res.detail
        except Exception as e:
            conformite = 0.0
            execution = 0.0
            detail = f"Erreur interne fonction score: {e}"
        sortie = res.stdout[:2500]
        erreur = ""

    # Affichage de la progression sur stderr
    sys.stderr.write(f"[PROG] {item_id} | niv:{niveau} | eff:{effort} | conf:{conformite:.2f} | exec:{execution:.2f} | lat:{res.latency_s:.2f}s\n")
    sys.stderr.flush()

    return {
        "id": item_id,
        "model": model,
        "epreuve": item["epreuve"],
        "niveau": niveau,
        "effort": effort,
        "tier": tier,
        "rc": res.rc,
        "latence_s": res.latency_s,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "tok_per_s": res.tok_per_s,
        "conformite": conformite,
        "execution": execution,
        "detail": detail,
        "sortie": sortie,
        "erreur": erreur
    }

def cmd_run(args):
    plan_path = Path(args.plan)
    out_path = Path(args.out)
    workers = args.workers

    with open(plan_path, "r", encoding="utf-8") as f:
        plan_data = json.load(f)

    budget_limit = plan_data.get("budget_tokens", 200000)
    budget = harness.TokenBudget(budget_limit)
    items = plan_data.get("items", [])

    completed_ids = get_completed_ids(out_path)
    pending_items = [item for item in items if item["id"] not in completed_ids]

    model_locks = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(execute_item, item, budget, model_locks): item for item in pending_items}
        for future in as_completed(futures):
            result = future.result()
            # Écriture thread-safe avec verrou
            with file_lock:
                with open(out_path, "a", encoding="utf-8") as f_out:
                    f_out.write(json.dumps(result, ensure_ascii=False) + "\n")
                    f_out.flush()

def cmd_grading_pack(args):
    results_path = Path(args.results)
    filter_ids = set(args.ids.split(",")) if args.ids else None
    max_chars = args.max_chars

    if not results_path.exists():
        return

    with open(results_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("erreur"):
                continue

            item_id = data["id"]
            if filter_ids and item_id not in filter_ids:
                continue

            niveau = data["niveau"]
            conf = data["conformite"]
            exec_val = data["execution"]
            epreuve_name = data["epreuve"]
            sortie = data.get("sortie", "")

            if len(sortie) > max_chars:
                sortie = sortie[:max_chars] + "... [tronqué]"

            try:
                ep = deep.get_epreuve(epreuve_name)
                rubric = ep.rubric(niveau)
            except Exception:
                rubric = "Rubrique non disponible."

            print(f"### {item_id} | niveau {niveau} | conformité {conf} | exécution {exec_val}")
            print(rubric)
            print(f"<sortie>\n{sortie}\n</sortie>\n")

def cmd_template_plan(args):
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    niveau = args.niveau
    effort = args.effort

    epreuves = list(deep.EPREUVES)

    items = []
    for model in models:
        model_short = model.split(":", 1)[-1].replace("/", "-").replace(":", "-")
        for ep_name in epreuves:
            item_id = f"{model_short}-{ep_name}-n{niveau}"
            items.append({
                "id": item_id,
                "model": model,
                "epreuve": ep_name,
                "niveau": niveau,
                "effort": effort,
                "tier": None,
                "timeout": 120,
                "note": f"Évaluation automatique {ep_name}"
            })

    plan = {
        "version": 1,
        "budget_tokens": 200000,
        "items": items
    }

    print(json.dumps(plan, ensure_ascii=False, indent=2))

def main():
    parser = argparse.ArgumentParser(description="Exécuteur de benchmarks approfondis vinyle Radar")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Commande run
    parser_run = subparsers.add_parser("run", help="Exécute le plan de benchmark")
    parser_run.add_argument("--plan", required=True, help="Chemin vers le fichier plan.json")
    parser_run.add_argument("--out", required=True, help="Chemin vers le fichier résultats jsonl")
    parser_run.add_argument("--workers", type=int, default=2, help="Nombre de threads max")

    # Commande grading-pack
    parser_grading = subparsers.add_parser("grading-pack", help="Génère le pack pour l'agent juge")
    parser_grading.add_argument("--results", required=True, help="Chemin vers les résultats jsonl")
    parser_grading.add_argument("--ids", default=None, help="IDs séparés par des virgules à filtrer")
    parser_grading.add_argument("--max-chars", type=int, default=1800, help="Nombre max de caractères pour la sortie")

    # Commande template-plan
    parser_template = subparsers.add_parser("template-plan", help="Génère un plan JSON de base")
    parser_template.add_argument("--models", required=True, help="Modèles séparés par des virgules")
    parser_template.add_argument("--niveau", type=int, default=2, help="Niveau de difficulté par défaut")
    parser_template.add_argument("--effort", default="medium", help="Niveau d'effort")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "grading-pack":
        cmd_grading_pack(args)
    elif args.command == "template-plan":
        cmd_template_plan(args)

if __name__ == "__main__":
    main()
