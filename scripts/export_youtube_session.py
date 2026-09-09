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
    1. Ferme TOUTES les fenêtres Chrome (le profil ne peut pas être ouvert deux fois).
    2. python3 scripts/export_youtube_session.py
    3. Le script propose un dossier de profil par défaut ; valide ou colle le tien
       (visible dans Chrome, à l'adresse chrome://version → « Chemin du profil » —
       prends le dossier PARENT de "Default"/"Profile X", pas ce sous-dossier lui-même).
    4. Une fenêtre Chrome s'ouvre déjà connectée. Vérifie que tu es bien sur YouTube
       connecté, reviens ici, appuie sur Entrée.

Le fichier `youtube_session.json` généré à côté de ce script est à importer dans Radar.

La session expire au bout d'un moment (durée variable côté Google) : si le nettoyage
signale une session expirée, relance simplement ce script et réimporte le nouveau
fichier."""
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "youtube_session.json")


def default_profile_dir():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    return os.path.join(home, ".config", "google-chrome")


def main():
    print("Ferme TOUTES les fenêtres Chrome avant de continuer "
          "(le profil ne peut pas être ouvert deux fois).")
    input("Entrée une fois Chrome fermé... ")

    default_dir = default_profile_dir()
    profile_dir = input(f"Dossier profil Chrome [{default_dir}] : ").strip() or default_dir
    if not os.path.isdir(profile_dir):
        print(f"Dossier introuvable : {profile_dir}")
        print("Trouve le bon chemin dans Chrome, à l'adresse chrome://version "
              "(« Chemin du profil » — prends le dossier PARENT de Default/Profile X).")
        return
    sub_profile = input("Sous-dossier de profil dans ce dossier [Default] : ").strip() or "Default"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir, channel="chrome", headless=False,
            args=[f"--profile-directory={sub_profile}"])
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.youtube.com")
            input("Vérifie que tu es bien connecté sur YouTube dans la fenêtre, "
                  "puis appuie sur Entrée ici... ")
            context.storage_state(path=OUT)
        finally:
            context.close()
    print(f"Session sauvegardée dans {OUT} — importe ce fichier dans Radar "
          f"(Mes sources → RECOS RADAR → Importer la session de visionnage).")


if __name__ == "__main__":
    main()
