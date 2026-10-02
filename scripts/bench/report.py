import argparse
import csv
import json
math_log = __import__('math').log

def note_item(conformite: float, execution: float, pertinence: float | None, erreur: bool) -> tuple[float, bool]:
    """Calcule la note sur 100 d'un item avec crédit partiel ou zéro si erreur."""
    if erreur:
        return 0.0, False
    if pertinence is None:
        # Note partielle normalisée sur 100 si la pertinence est absente
        note_partielle = ((15.0 * conformite + 45.0 * execution) / 60.0) * 100.0
        return round(note_partielle, 2), True
    
    note = 15.0 * conformite + 45.0 * execution + 4.0 * pertinence
    return round(note, 2), False

def note_vitesse(t: float, tmin: float, tmax: float) -> float:
    """Calcule la note de vitesse sur 20 en échelle logarithmique."""
    if tmin <= 0 or t <= 0:
        return 0.0
    if tmax == tmin:
        return 20.0
    if t <= tmin:
        return 20.0
    if t >= tmax:
        return 0.0
    
    val = 20.0 * (1.0 - math_log(t / tmin) / math_log(tmax / tmin))
    return max(0.0, min(20.0, round(val, 2)))

def aggregate(items: list[dict], grades: dict) -> tuple[list[dict], list[dict]]:
    """Aggrège les items et les notes pour produire les métriques par item et par modèle."""
    processed_items = []
    for item in items:
        item_id = str(item.get("id"))
        grade_info = grades.get(item_id, {})
        pertinence = grade_info.get("pertinence")
        justification = grade_info.get("justification", "")
        
        rc = item.get("rc", 0)
        err_field = item.get("erreur", "")
        is_error = (rc != 0) or bool(err_field)
        
        conf = float(item.get("conformite", 0.0))
        exec_val = float(item.get("execution", 0.0))
        
        note, non_note = note_item(conf, exec_val, pertinence, is_error)
        
        processed_items.append({
            "id": item_id,
            "modele": item.get("model", "inconnu"),
            "epreuve": item.get("epreuve", ""),
            "niveau": item.get("niveau", 1),
            "effort": item.get("effort", ""),
            "conformite": conf,
            "execution": exec_val,
            "pertinence": pertinence if pertinence is not None else "",
            "note": note,
            "non_note": non_note,
            "justification": justification.replace("\n", " ").replace("\r", " ")[:120],
            "latence_s": float(item.get("latence_s", 0.0)),
            "tokens_totaux": int(item.get("tokens_in", 0)) + int(item.get("tokens_out", 0)),
            "tok_per_s": float(item.get("tok_per_s", 0.0)),
        })

    # Agrégation par modèle
    models_data = {}
    for it in processed_items:
        m = it["modele"]
        if m not in models_data:
            models_data[m] = {
                "notes": [],
                "latences": [],
                "tokens": 0,
                "tok_s_list": [],
                "efforts": set(),
                "non_notes_count": 0,
            }
        models_data[m]["notes"].append(it["note"])
        models_data[m]["latences"].append(it["latence_s"])
        models_data[m]["tokens"] += it["tokens_totaux"]
        models_data[m]["tok_s_list"].append(it["tok_per_s"])
        models_data[m]["efforts"].add(str(it["effort"]))
        if it["non_note"]:
            models_data[m]["non_notes_count"] += 1

    model_summary_raw = []
    latences_moyennes = []
    for m, data in models_data.items():
        qualite_100 = sum(data["notes"]) / len(data["notes"]) if data["notes"] else 0.0
        latence_moy = sum(data["latences"]) / len(data["latences"]) if data["latences"] else 0.0
        tok_s_moy = sum(data["tok_s_list"]) / len(data["tok_s_list"]) if data["tok_s_list"] else 0.0
        
        model_summary_raw.append({
            "modele": m,
            "qualite_100": round(qualite_100, 2),
            "latence_moy": latence_moy,
            "jetons_totaux": data["tokens"],
            "tok_per_s_moy": round(tok_s_moy, 2),
            "efforts_utilises": ",".join(sorted(list(data["efforts"]))),
            "nb_items_non_notes": data["non_notes_count"],
        })
        latences_moyennes.append(latence_moy)

    tmin = min(latences_moyennes) if latences_moyennes else 0.0
    tmax = max(latences_moyennes) if latences_moyennes else 0.0

    model_summary = []
    for ms in model_summary_raw:
        lat_moy = ms["latence_moy"]
        vitesse_20 = note_vitesse(lat_moy, tmin, tmax)
        qualite_20 = ms["qualite_100"] / 5.0
        final_20 = (1.0 * vitesse_20 + 2.0 * qualite_20) / 3.0
        
        model_summary.append({
            "modele": ms["modele"],
            "qualite_100": ms["qualite_100"],
            "vitesse_20": vitesse_20,
            "final_20": round(final_20, 2),
            "latence_moyenne": round(lat_moy, 2),
            "jetons_totaux": ms["jetons_totaux"],
            "tok_per_s_moyen": ms["tok_per_s_moy"],
            "efforts_utilises": ms["efforts_utilises"],
            "nb_items_non_notes": ms["nb_items_non_notes"],
        })

    # Tri par FINAL décroissant
    model_summary.sort(key=lambda x: x["final_20"], reverse=True)
    
    # Ajout du rang
    for idx, row in enumerate(model_summary, start=1):
        row["rang"] = idx

    return model_summary, processed_items

def main():
    parser = argparse.ArgumentParser(description="Fusion des résultats et notes du juge pour le benchmark.")
    parser.add_argument("--results", default="results.jsonl", help="Chemin vers le fichier JSONL des résultats.")
    parser.add_argument("--grades", default="grades.json", help="Chemin vers le fichier JSON des notes du juge.")
    parser.add_argument("--out-prefix", default="./bench_deep", help="Préfixe pour les fichiers CSV de sortie.")
    args = parser.parse_args()

    items = []
    with open(args.results, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))

    grades = {}
    try:
        with open(args.grades, "r", encoding="utf-8") as f:
            grades = json.load(f)
    except FileNotFoundError:
        pass

    model_summary, processed_items = aggregate(items, grades)

    # Écriture des modèles
    path_modeles = f"{args.out_prefix}_modeles.csv"
    with open(path_modeles, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rang", "modèle", "qualité/100", "vitesse/20", "FINAL/20", 
            "latence moyenne", "jetons totaux", "tok/s moyen", 
            "efforts utilisés", "nb d'items non notés"
        ])
        for m in model_summary:
            writer.writerow([
                m["rang"], m["modele"], m["qualite_100"], m["vitesse_20"], m["final_20"],
                m["latence_moyenne"], m["jetons_totaux"], m["tok_per_s_moyen"],
                m["efforts_utilises"], m["nb_items_non_notes"]
            ])

    # Écriture des items
    path_items = f"{args.out_prefix}_items.csv"
    with open(path_items, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "modèle", "niveau", "effort", "conformité", 
            "exécution", "pertinence", "note", "justification"
        ])
        for it in processed_items:
            writer.writerow([
                it["id"], it["modele"], it["niveau"], it["effort"], 
                it["conformite"], it["execution"], it["pertinence"], 
                it["note"], it["justification"]
            ])

if __name__ == "__main__":
    main()
