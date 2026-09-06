"""À lancer EN LOCAL (Mac), pas sur le VPS — ouvre un vrai Chrome pour que tu te
connectes à YouTube à la main, puis sauvegarde la session dans un fichier à importer
dans Radar (Mes sources → RECOS RADAR → « Importer la session de visionnage »).

Sert au nettoyage automatique de la playlist RECOS RADAR (retire ce que tu as déjà
écouté) : Radar rejoue cette session en tâche de fond pour lire ta page d'historique
YouTube, plutôt que d'automatiser un vrai login Google (fragile, détection anti-bot).

Usage :
    pip install playwright
    playwright install chromium
    python3 scripts/export_youtube_session.py

Une fenêtre Chrome s'ouvre sur la page de connexion Google. Connecte-toi normalement
(mot de passe, 2FA si tu en as une), attends d'arriver sur ta page d'accueil YouTube,
puis reviens ici et appuie sur Entrée. Le fichier `youtube_session.json` généré à côté
de ce script est à importer dans Radar.

La session expire au bout d'un moment (durée variable côté Google) : si le nettoyage
signale une session expirée, relance simplement ce script et réimporte le nouveau
fichier."""
import os

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "youtube_session.json")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.youtube.com")
        input("Connecte-toi à YouTube dans la fenêtre Chrome, puis appuie sur Entrée ici... ")
        context.storage_state(path=OUT)
        browser.close()
    print(f"Session sauvegardée dans {OUT} — importe ce fichier dans Radar "
          f"(Mes sources → RECOS RADAR → Importer la session de visionnage).")


if __name__ == "__main__":
    main()
