"""À lancer EN LOCAL (ta machine), pas sur le VPS — réutilise ton VRAI profil Chrome
(déjà connecté à YouTube au quotidien) pour générer une session à importer dans Radar
(Mes sources → RECOS RADAR → « Importer la session de visionnage »).

Sert au nettoyage automatique de la playlist RECOS RADAR (retire ce que tu as déjà
écouté) : Radar rejoue cette session en tâche de fond pour lire ta page d'historique
YouTube, plutôt que d'automatiser un vrai login Google.

Pourquoi le profil réel et pas un Chrome vierge : un Chrome piloté par Playwright et
dirigé vers la page de connexion Google est détecté comme automatisé et refusé
(« ce navigateur ou cette application n'est pas sécurisée ») — Google bloque la
tentative de connexion elle-même, indépendamment de l'OS ou de l'IP. En pointant
Playwright sur un profil Chrome où tu es DÉJÀ connecté (usage normal, au quotidien),
aucune connexion n'est tentée dans l'automation : rien à bloquer.

Pourquoi une COPIE du profil et pas l'original directement : Chrome refuse d'activer
le pilotage à distance (nécessaire à Playwright) quand `--user-data-dir` pointe vers
l'emplacement par défaut du système (mesure de sécurité anti-malware — retour terrain :
« DevTools remote debugging requires a non-default data directory »). Le script copie
donc le profil (hors caches, pour rester rapide) dans un dossier temporaire, lance
Chrome dessus, puis supprime la copie à la fin.

Usage :
    pip install playwright
    playwright install chromium
    1. Ferme TOUTES les fenêtres Chrome ET vérifie qu'aucun processus chrome.exe ne
       tourne encore en arrière-plan (Gestionnaire des tâches sous Windows — Chrome
       laisse souvent un processus actif après fermeture des fenêtres, ce qui verrouille
       le profil et fait planter ce script). Sous Windows : `taskkill /F /IM chrome.exe`
       dans un terminal, à refaire jusqu'à ce qu'il ne trouve plus rien.
    2. python3 scripts/export_youtube_session.py
    3. Le script propose un dossier de profil par défaut ; valide ou colle le tien
       (visible dans Chrome, à l'adresse chrome://version → « Chemin du profil »).
       Colle le chemin TEL QUEL affiché par chrome://version (le script sépare
       lui-même le sous-dossier "Default"/"Profile X" du reste).
    4. Le script copie le profil (peut prendre quelques dizaines de secondes) puis
       ouvre une fenêtre Chrome déjà connectée. Vérifie que tu es bien sur YouTube
       connecté, reviens ici, appuie sur Entrée.

Le fichier `youtube_session.json` généré à côté de ce script est à importer dans Radar.

La session expire au bout d'un moment (durée variable côté Google) : si le nettoyage
signale une session expirée, relance simplement ce script et réimporte le nouveau
fichier."""
import os
import re
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "youtube_session.json")
_PROFILE_SUBDIR_RE = re.compile(r"^(Default|Profile \d+)$", re.I)
# Dossiers de cache : volumineux (peuvent faire plusieurs Go), inutiles pour une
# session de connexion — exclus de la copie pour rester rapide.
_IGNORE_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
                "ShaderCache", "GrShaderCache", "CacheStorage", "component_crx_cache",
                "extensions_crx_cache", "Crashpad", "BrowserMetrics",
                "optimization_guide_model_store"}


def _safe_copy(src, dst):
    """Comme shutil.copy2, mais un fichier verrouillé (résidu Chrome pas
    complètement fermé) est juste ignoré plutôt que de faire échouer toute la
    copie — on n'a besoin que des cookies/stockage local, pas de 100% du profil."""
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        print(f"  (ignoré, verrouillé : {os.path.basename(src)} — {e})")


def copy_profile(src_root, tmp_root):
    """Copie src_root (dossier "User Data") vers tmp_root, hors caches — inclut
    "Local State" à la racine (clé de déchiffrement des cookies) et le(s)
    sous-profil(s)."""
    shutil.copytree(src_root, tmp_root, dirs_exist_ok=True,
                     ignore=shutil.ignore_patterns(*_IGNORE_DIRS),
                     copy_function=_safe_copy)


def default_profile_dir():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    return os.path.join(home, ".config", "google-chrome")


def split_profile_path(path):
    """Sépare un chemin collé depuis chrome://version en (dossier User Data,
    sous-dossier Default/Profile X) — que l'utilisateur colle le dossier parent
    ou le chemin complet jusqu'au sous-profil, le résultat est le même
    (cf. retour terrain : chemin complet collé deux fois -> Default/Default
    inexistant -> crash silencieux du navigateur)."""
    path = path.rstrip("\\/")
    base = os.path.basename(path)
    if _PROFILE_SUBDIR_RE.match(base):
        return os.path.dirname(path), base
    return path, "Default"


def main():
    print("Ferme TOUTES les fenêtres Chrome avant de continuer, ET vérifie qu'aucun "
          "processus chrome.exe ne tourne encore en arrière-plan (Gestionnaire des "
          "tâches) — sinon le profil reste verrouillé et le script plante.")
    input("Entrée une fois Chrome complètement fermé... ")

    default_dir = default_profile_dir()
    raw = input(f"Dossier profil Chrome (chrome://version) [{default_dir}] : ").strip() or default_dir
    profile_dir, sub_profile = split_profile_path(raw)
    if not os.path.isdir(os.path.join(profile_dir, sub_profile)):
        print(f"Dossier introuvable : {os.path.join(profile_dir, sub_profile)}")
        print("Colle le chemin exact affiché par Chrome à l'adresse chrome://version, "
              "champ « Chemin du profil ».")
        return
    print(f"Profil source : {profile_dir} (sous-profil « {sub_profile} »)")

    tmp_root = tempfile.mkdtemp(prefix="radar_chrome_profile_")
    print(f"Copie du profil vers {tmp_root} (hors caches, patiente quelques secondes)...")
    try:
        copy_profile(profile_dir, tmp_root)
    except OSError as e:
        print(f"Échec de la copie du profil ({type(e).__name__}: {e}).")
        shutil.rmtree(tmp_root, ignore_errors=True)
        return

    try:
        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    tmp_root, channel="chrome", headless=False,
                    args=[f"--profile-directory={sub_profile}"])
            except Exception as e:
                print(f"Échec du lancement de Chrome ({type(e).__name__}: {e}) — vérifie "
                      "qu'aucun processus chrome.exe ne tourne encore (Gestionnaire des tâches).")
                return
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.youtube.com")
                input("Vérifie que tu es bien connecté sur YouTube dans la fenêtre, "
                      "puis appuie sur Entrée ici... ")
                context.storage_state(path=OUT)
            except Exception as e:
                print(f"La fenêtre Chrome s'est fermée ou a planté avant la fin "
                      f"({type(e).__name__}: {e}) — relance le script.")
                return
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"Session sauvegardée dans {OUT} — importe ce fichier dans Radar "
          f"(Mes sources → RECOS RADAR → Importer la session de visionnage).")


if __name__ == "__main__":
    main()
