"""Capture une session Discogs connectée, une bonne fois.

À lancer SUR TON MAC (pas sur le VPS). Ouvre un Chromium visible ; tu te
connectes à la main (coche « Keep me logged in ») ; le script enregistre la
session dans ./discogs_state.json.

Ensuite, envoie ce fichier à Radar :
  - soit via Réglages → « Session Discogs » (upload dans l'appli), la plus simple ;
  - soit :  scp discogs_state.json ubuntu@57.128.180.93:/tmp/discogs_state.json
            ssh ubuntu@57.128.180.93 'sudo mv /tmp/discogs_state.json ~/radar/data/'

Les jobs (market_fr, fenêtre marketplace) réutilisent la session tant qu'elle est
valide — plusieurs semaines avec « keep me logged in ». À refaire seulement quand
Discogs la coupe.

Prérequis (Mac, une fois) :
  python3 -m pip install --user "playwright==1.44.0"
  python3 -m playwright install chromium
"""
import sys

from playwright.sync_api import sync_playwright

OUT = "discogs_state.json"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="fr-FR", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto("https://www.discogs.com/login", wait_until="domcontentloaded")
        print("\n>>> Connecte-toi dans la fenêtre Chromium (coche « Keep me logged in »).")
        print(">>> Quand ton compte est connecté (menu en haut à droite), reviens ici")
        print(">>> et appuie sur Entrée pour enregistrer la session.\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        # ne garde que discogs.com + cloudflare
        state = ctx.storage_state()
        state["cookies"] = [c for c in state.get("cookies", [])
                            if "discogs.com" in c.get("domain", "")]
        import json
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        signed = any(c["name"] in ("sgp", "session") for c in state["cookies"])
        print(f">>> Écrit {OUT} — {len(state['cookies'])} cookies discogs.com"
              f"{' · session détectée ✅' if signed else ' · ⚠️ pas de cookie de session, tu étais peut-être pas connecté'}")
        ctx.close()
        b.close()


if __name__ == "__main__":
    sys.exit(main())
