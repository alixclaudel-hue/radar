"""Ouvre un Chromium visible (via VNC) sur la page de connexion Discogs, avec un
profil PERSISTANT dans /data/discogs_profile. Tu te connectes une fois à la main
(coche « Rester connecté »), puis tu fermes la fenêtre ou Ctrl-C.

Ensuite les jobs headless réutilisent ce profil : plus de mot de passe stocké,
plus de cookie à recoller — jusqu'à ce que Discogs invalide la session (rare).

Lancé par tools/vnc_login.sh.
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

PROFILE = os.environ.get("DISCOGS_PROFILE", "/data/discogs_profile")


def main():
    os.makedirs(PROFILE, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
            viewport={"width": 1280, "height": 860},
            locale="fr-FR",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.discogs.com/login", wait_until="domcontentloaded")
        print(">>> Connecte-toi dans la fenêtre (coche « Keep me logged in »).")
        print(">>> Quand le menu de ton compte apparaît en haut à droite, tu peux "
              "fermer la fenêtre ou faire Ctrl-C ici.", flush=True)
        # attend soit la fermeture manuelle, soit la détection d'un cookie de session
        try:
            while True:
                time.sleep(3)
                names = {c["name"] for c in ctx.cookies()}
                if {"sgp", "session"} & names or "__cf_bm" in names and len(names) > 6:
                    print(">>> Session détectée. Encore 20 s pour finir, puis fermeture.",
                          flush=True)
                    time.sleep(20)
                    break
        except KeyboardInterrupt:
            pass
        finally:
            ctx.close()
    print(">>> Profil enregistré dans", PROFILE)


if __name__ == "__main__":
    sys.exit(main())
