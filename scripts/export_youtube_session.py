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
    4. Une fenêtre Chrome s'ouvre déjà connectée. Vérifie que tu es bien sur YouTube
       connecté, reviens ici, appuie sur Entrée.

Le fichier `youtube_session.json` généré à côté de ce script est à importer dans Radar.

La session expire au bout d'un moment (durée variable côté Google) : si le nettoyage
signale une session expirée, relance simplement ce script et réimporte le nouveau
fichier."""
import os
import re
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "youtube_session.json")
_PROFILE_SUBDIR_RE = re.compile(r"^(Default|Profile \d+)$", re.I)


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
    print(f"Profil utilisé : {profile_dir} (sous-profil « {sub_profile} »)")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                profile_dir, channel="chrome", headless=False,
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
                  f"({type(e).__name__}: {e}) — vérifie qu'aucun autre processus "
                  "chrome.exe ne verrouillait le profil, puis relance.")
            return
        finally:
            try:
                context.close()
            except Exception:
                pass
    print(f"Session sauvegardée dans {OUT} — importe ce fichier dans Radar "
          f"(Mes sources → RECOS RADAR → Importer la session de visionnage).")


if __name__ == "__main__":
    main()
