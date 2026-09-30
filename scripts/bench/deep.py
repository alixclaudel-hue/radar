from dataclasses import dataclass
import json
import subprocess
import sys
import tempfile
import os
import difflib
from typing import Callable, Any
from scripts.bench.harness import CallResult, call_broker

@dataclass(frozen=True)
class DetScore:
    conformite: float
    execution: float
    detail: dict

@dataclass(frozen=True)
class Epreuve:
    id: str
    titre: str
    mode: str
    base_args: tuple[str, ...]
    build_prompt: Callable[[int], str]
    score: Callable[[str, int], DetScore]
    rubric: Callable[[int], str]

REFERENCE_CODE = '''def planifier(taches, capacite):\n    """Répartit des tâches (nom, durée) sur des jours de `capacite` heures en gardant l'ordre ; renvoie une liste de jours (listes de noms)."""\n    if capacite <= 0:\n        raise ValueError("capacité invalide")\n    jours, courant, charge = [], [], 0\n    for nom, duree in taches:\n        if duree > capacite:\n            raise ValueError(f"tâche trop longue : {nom}")\n        if charge + duree > capacite:\n            jours.append(courant)\n            courant, charge = [], 0\n    """\n        courant.append(nom)\n        charge += duree\n    if courant:\n        jours.append(courant)\n    return jours\n'''

# Fonction de reference exacte pour construire les bugs
def _reference_planifier(taches, capacite):
    if capacite <= 0:
        raise ValueError("capacité invalide")
    jours, courant, charge = [], [], 0
    for nom, duree in taches:
        if duree > capacite:
            raise ValueError(f"tâche trop longue : {nom}")
        if charge + duree > capacite:
            jours.append(courant)
            courant, charge = [], 0
        courant.append(nom)
        charge += duree
    if courant:
        jours.append(courant)
    return jours

def apply_bugs(niveau: int) -> tuple[str, list[str]]:
    # Applique les bugs au code de reference par substitution textuelle
    code = inspect_source_clean()
    bugs_desc = []
    if niveau >= 1:
        # Bug B : supprimer le bloc final
        code = code.replace("    if courant:\n        jours.append(courant)", "    # bug_b_supprime")
        bugs_desc.append("Bloc final de vidange de courant supprime (fin de fonction)")
    if niveau >= 2:
        # Bug A : remplacer '>' par '>=' dans if charge + duree > capacite
        code = code.replace("if charge + duree > capacite:", "if charge + duree >= capacite:")
        bugs_desc.append("Condition de surcharge modifiee de '>' a '>=' dans la boucle")
    if niveau >= 3:
        # Bug C : remplacer courant, charge = [], 0 par courant = []
        code = code.replace("courant, charge = [], 0", "courant = []")
        bugs_desc.append("Reinitialisation de la charge oubliee lors du passage au jour suivant")
    return code, bugs_desc

def inspect_source_clean() -> str:
    return (
        "def planifier(taches, capacite):\n"
        "    if capacite <= 0:\n"
        "        raise ValueError(\"capacité invalide\")\n"
        "    jours, courant, charge = [], [], 0\n"
        "    for nom, duree in taches:\n"
        "        if duree > capacite:\n"
        "            raise ValueError(f\"tâche trop longue : {nom}\")\n"
        "        if charge + duree > capacite:\n"
        "            jours.append(courant)\n"
        "            courant, charge = [], 0\n"
        "        courant.append(nom)\n"
        "        charge += duree\n"
        "    if courant:\n"
        "        jours.append(courant)\n"
        "    return jours\n"
    )

def build_prompt_debug(niveau: int) -> str:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    buggy_code, _ = apply_bugs(niveau)
    return (
        "Tu es un ouvrier de code. Cette fonction contient un ou plusieurs bugs. "
        "Réponds par UN bloc de code python avec la fonction corrigée complète, puis en 3 phrases maximum : "
        "chaque bug identifié (où, pourquoi). Maximum 500 jetons.\n\n"
        f"{buggy_code}"
    )

def score_debug(sortie: str, niveau: int) -> DetScore:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    
    # Extraction du premier bloc de code entre backticks
    code_extrait = ""
    if "```python" in sortie:
        parts = sortie.split("```python")
        if len(parts) > 1:
            code_extrait = parts[1].split("```")[0].strip()
    elif "```" in sortie:
        parts = sortie.split("```")
        if len(parts) > 1:
            code_extrait = parts[1].split("```")[0].strip()
    else:
        code_extrait = sortie.strip()

    # Verification de la presence d un bloc de code
    has_block = 1.0 if code_extrait else 0.0
    
    # Test de compilation
    compiles = 0.0
    try:
        compile(code_extrait, "<string>", "exec")
        compiles = 1.0
    except Exception:
        compiles = 0.0

    # Minimalite via SequenceMatcher par rapport au code de reference
    ref_clean = inspect_source_clean()
    matcher = difflib.SequenceMatcher(None, code_extrait, ref_clean)
    ratio = matcher.ratio()
    minimalite = max(0.0, min(1.0, (ratio - 0.5) / 0.5))

    conformite = (compiles + has_block + minimalite) / 3.0

    # Execution de 8 tests independants dans un sous-processus
    test_script = (
        "import json\n"
        f"{code_extrait}\n"
        "res = []\n"
        "# 1\n"
        "try:\n    res.append(planifier([('a',2),('b',2)],4)==[['a','b']])\n"
        "except Exception:\n    res.append(False)\n"
        "# 2\n"
        "try:\n    res.append(planifier([('a',3),('b',3),('c',2)],6)==[['a','b'],['c']])\n"
        "except Exception:\n    res.append(False)\n"
        "# 3\n"
        "try:\n    res.append(planifier([('a',1)],4)==[['a']])\n"
        "except Exception:\n    res.append(False)\n"
        "# 4\n"
        "try:\n    res.append(planifier([],4)==[])\n"
        "except Exception:\n    res.append(False)\n"
        "# 5\n"
        "try:\n    res.append(planifier([('a',3),('b',3),('c',3),('d',3)],6)==[['a','b'],['c','d']])\n"
        "except Exception:\n    res.append(False)\n"
        "# 6\n"
        "try:\n    res.append(planifier([('a',2),('b',5)],5)==[['a'],['b']])\n"
        "except Exception:\n    res.append(False)\n"
        "# 7\n"
        "try:\n    planifier([('a',1)],0)\n    res.append(False)\n"
        "except ValueError:\n    res.append(True)\n"
        "except Exception:\n    res.append(False)\n"
        "# 8\n"
        "try:\n    planifier([('a',9)],5)\n    res.append(False)\n"
        "except ValueError:\n    res.append(True)\n"
        "except Exception:\n    res.append(False)\n"
        "print(json.dumps({'ok': res}))\n"
    )

    ok_results = [False] * 8
    if compiles > 0:
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tf:
                tf.write(test_script)
                tf_name = tf.name
            proc = subprocess.run([sys.executable, tf_name], capture_output=True, text=True, timeout=10.0)
            os.unlink(tf_name)
            if proc.returncode == 0:
                data = json.loads(proc.stdout.strip())
                ok_results = data.get('ok', [False]*8)
        except Exception:
            if 'tf_name' in locals() and os.path.exists(tf_name):
                os.unlink(tf_name)

    reussites = sum(1 for x in ok_results if x)
    execution = reussites / 8.0

    detail = {
        "compiles": compiles,
        "has_block": has_block,
        "minimalite": minimalite,
        "ok": ok_results,
        "reussites": reussites
    }
    return DetScore(conformite=conformite, execution=execution, detail=detail)

def rubric_debug(niveau: int) -> str:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    _, bugs = apply_bugs(niveau)
    bugs_str = ", ".join(bugs)
    return (
        f"Bugs réellement présents : {bugs_str}. "
        "Note 0-10 : 10 = tous les bugs présents identifiés avec la bonne cause, correction minimale, aucune affirmation fausse ; "
        "retire 2 points par bug manqué, 2 par bug inventé, 2 si réécriture inutile, 2 si explication absente ou vague."
    )

# EPREUVE DIAGNOSTIC_LOG

def generate_log(niveau: int) -> tuple[str, list[int]]:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    
    # Noyau de base fixe et deterministe
    lignes_neutres = [
        "10:00:01 INFO cache init demarrage reussi",
        "10:00:02 INFO scan indexation terminee 42 fichiers",
        "10:00:03 INFO cache verification integite OK",
        "10:00:04 INFO scan surveillance active"
    ]
    
    # Construction du journal selon le niveau
    if niveau == 1:
        neutres = lignes_neutres * 5 # 20 lignes
    elif niveau == 2:
        neutres = lignes_neutres * 7 + lignes_neutres[:2] # 30 lignes
    else:
        neutres = lignes_neutres * 10 # 40 lignes

    log_lines = list(neutres)
    relevantes = []

    # Insertion des lignes cles aux positions fixes
    log_lines.insert(5, "10:00:05 WARN broker gemini HTTP 429 quota par minute dépassé (modèle gemini-3.5-flash-lite)")
    log_lines.insert(12, "10:00:12 WARN broker gemini-3.5-flash-lite mis en cooldown 63s")

    # Insertion des leurres selon le niveau
    if niveau >= 1:
        log_lines.insert(2, "10:00:02 ERROR web favicon.ico 404")
    if niveau >= 2:
        log_lines.insert(18, "10:00:18 WARN disk usage 71%")
        log_lines.insert(25, "10:00:25 ERROR ytcache clé YouTube expirée")
    if niveau >= 3:
        log_lines.append("10:00:35 ERROR judge aucun candidat utilisable")
        log_lines.append("10:00:36 ERROR bench appel refusé 503")

    # Numerotation 1-based des lignes finales
    formatted = []
    relevantes_indices = []
    for idx, l in enumerate(log_lines, start=1):
        formatted.append(f"{idx:02d}| {l}")
        # Identifier si la ligne est une cause/cle ou un leurre/symptome
        if "429" in l or "cooldown" in l:
            relevantes_indices.append(idx)

    return "\n".join(formatted), relevantes_indices

def build_prompt_diagnostic(niveau: int) -> str:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    log_str, _ = generate_log(niveau)
    return (
        "Tu es un ouvrier de diagnostic. Voici un extrait de journal, lignes numérotées. Trouve la cause racine. "
        "Réponds UNIQUEMENT par un objet JSON {\"cause_racine\": str, \"preuves\": [numéros de lignes int], \"correctif\": str, \"risque\": str}. "
        "Phrases courtes, 500 jetons maximum.\n\n"
        f"{log_str}"
    )

def score_diagnostic(sortie: str, niveau: int) -> DetScore:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    
    _, relevantes = generate_log(niveau)
    
    # Analyse conformite JSON
    clean_sortie = sortie.strip()
    if clean_sortie.startswith("```json"):
        clean_sortie = clean_sortie.split("```json")[1].split("```")[0].strip()
    elif clean_sortie.startswith("```"):
        clean_sortie = clean_sortie.split("```")[1].split("```")[0].strip()

    json_ok = 0.0
    data = {}
    try:
        data = json.loads(clean_sortie)
        json_ok = 1.0
    except Exception: # noqa: E722
        json_ok = 0.0
        # Extraction tolérante du premier { au dernier } : un JSON correct noyé dans du texte ne doit pas annuler l'exécution, le défaut de format n'est sanctionné qu'une fois par json_ok ; sinon la notation redevient binaire
        debut, fin = clean_sortie.find('{'), clean_sortie.rfind('}')
        if 0 <= debut < fin:
            try:
                data = json.loads(clean_sortie[debut:fin + 1])
            except Exception: # noqa: E722
                data = {}

    keys_ok = 0.0
    required_keys = {"cause_racine", "preuves", "correctif", "risque"}
    if isinstance(data, dict) and required_keys.issubset(data.keys()):
        keys_ok = 1.0

    types_ok = 0.0
    if keys_ok:
        cr = data.get("cause_racine")
        pr = data.get("preuves")
        co = data.get("correctif")
        ri = data.get("risque")
        if isinstance(cr, str) and isinstance(pr, list) and isinstance(co, str) and isinstance(ri, str):
            if all(isinstance(x, int) for x in pr):
                types_ok = 1.0

    non_empty = 0.0
    if types_ok:
        if data["cause_racine"].strip() and data["correctif"].strip() and data["risque"].strip():
            non_empty = 1.0

    conformite = (json_ok + keys_ok + types_ok + non_empty) / 4.0

    # Execution / Evaluation metier
    cause_ok = 0.0
    precision = 0.0
    rappel = 0.0
    correctif_ok = 0.0

    if types_ok:
        cr_lower = data["cause_racine"].lower()
        has_core = ("429" in cr_lower) or ("quota" in cr_lower) or ("cooldown" in cr_lower)
        if niveau == 3:
            # Ne doit pas citer 503 ou judge comme cause
            if "503" in cr_lower or "judge" in cr_lower:
                has_core = False
        if has_core:
            cause_ok = 1.0

        preuves_citees = data["preuves"]
        if preuves_citees:
            # verif existence et pertinence
            # On considere que les lignes totales vont jusqu a 42 max
            valides = [p for p in preuves_citees if p in relevantes]
            precision = len(valides) / len(preuves_citees)
            rappel = len(set(preuves_citees).intersection(set(relevantes))) / len(relevantes)
        else:
            precision = 0.0
            rappel = 0.0

        co_lower = data["correctif"].lower()
        mots_cles = ['backoff', 'attente', 'espacer', 'limiter', 'sérialis', 'réessai', 'quota', 'parallèl']
        if any(m in co_lower for m in mots_cles):
            correctif_ok = 1.0

    execution = (cause_ok + precision + rappel + correctif_ok) / 4.0

    detail = {
        "json_ok": json_ok,
        "keys_ok": keys_ok,
        "types_ok": types_ok,
        "non_empty": non_empty,
        "cause_ok": cause_ok,
        "precision": precision,
        "rappel": rappel,
        "correctif_ok": correctif_ok
    }
    return DetScore(conformite=conformite, execution=execution, detail=detail)

def rubric_diagnostic(niveau: int) -> str:
    if niveau not in (1, 2, 3):
        raise ValueError("Niveau invalide")
    symptomes_txt = "les symptômes et leurres du niveau"
    if niveau == 1:
        symptomes_txt = "le 404 favicon"
    elif niveau == 2:
        symptomes_txt = "le 404 favicon, l'alerte disque et la clé YouTube expirée"
    elif niveau == 3:
        symptomes_txt = "le 404 favicon, l'alerte disque, la clé YouTube expirée, l'erreur judge et le refus 503"
    return (
        f"Cause réelle : quota 429 par minute du modèle gemini-3.5-flash-lite, suivi d'un cooldown de 63 s ; "
        f"{symptomes_txt} ne sont PAS la cause. Note 0-10 : 10 = cause correcte et distinguée des symptômes, "
        "correctif concret et réaliste (espacer/limiter/changer de juge), risque pertinent ; "
        "retire 3 points si un symptôme ou un leurre est pris pour la cause, 2 si correctif vague, 2 si risque hors sujet, 2 si affirmation non étayée par le log."
    )

EPREUVES: dict[str, Epreuve] = {
    'debug_multi_bugs': Epreuve(
        id='debug_multi_bugs',
        titre='Debug Multi-Bugs de Planification',
        mode='code',
        base_args=('--check-syntax', '--max-repairs', '0'),
        build_prompt=build_prompt_debug,
        score=score_debug,
        rubric=rubric_debug
    ),
    'diagnostic_log': Epreuve(
        id='diagnostic_log',
        titre='Diagnostic de Journal dErreurs',
        mode='general',
        base_args=('--json-output', '--max-repairs', '0'),
        build_prompt=build_prompt_diagnostic,
        score=score_diagnostic,
        rubric=rubric_diagnostic
    )
}

def get_epreuve(id: str) -> Epreuve:
    if id not in EPREUVES:
        raise ValueError(f"Epreuve inconnue : {id}")
    return EPREUVES[id]
