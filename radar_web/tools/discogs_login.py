"""Capture une session Discogs connectée, une bonne fois.

À lancer SUR TON MAC (pas sur le VPS). Ouvre un navigateur visible ; tu te
connectes à la main (coche « Keep me logged in ») ; le script enregistre la
session dans ./discogs_state.json.

Il essaie d'abord de piloter ton **Google Chrome** installé (bien mieux vu par
Cloudflare que le Chromium de Playwright), puis Edge, puis le Chromium fourni.

Si la page « Vérification de sécurité » s'affiche :
  - clique la case « Je ne suis pas un robot » si elle apparaît, puis patiente ;
  - si elle boucle, ferme, relance, et va d'abord sur discogs.com (pas /login)
    avant de te connecter.

Ensuite : Réglages → « Session Discogs » → charge discogs_state.json.

Prérequis (Mac) : python3 -m pip install --user "playwright==1.44.0"
(pas besoin de « playwright install » si Chrome est présent.)
"""
import json
import sys

from playwright.sync_api import sync_playwright

OUT = "discogs_state.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _launch(p):
    for ch in ("chrome", "msedge", None):
        try:
            b = p.chromium.launch(channel=ch, headless=False) if ch \
                else p.chromium.launch(headless=False)
            print(f">>> Navigateur : {ch or 'chromium (fourni)'}")
            return b
        except Exception:
            continue
    raise SystemExit("Aucun navigateur lançable. Installe Chrome, ou : "
                     "python3 -m playwright install chromium")


def main():
    with sync_playwright() as p:
        b = _launch(p)
        ctx = b.new_context(locale="fr-FR", user_agent=UA,
                            viewport={"width": 1280, "height": 860})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        page.goto("https://www.discogs.com/", wait_until="domcontentloaded")
        print("\n>>> 1) Si une page de vérification s'affiche, résous-la (case à cocher),")
        print(">>>    attends qu'elle disparaisse.")
        print(">>> 2) Connecte-toi à Discogs (menu en haut à droite), coche « Keep me logged in ».")
        print(">>> 3) Reviens ici et appuie sur Entrée.\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        state = ctx.storage_state()
        state["cookies"] = [c for c in state.get("cookies", [])
                            if "discogs.com" in c.get("domain", "")]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        signed = any(c["name"] in ("sgp", "session") for c in state["cookies"])
        print(f"\n>>> Écrit {OUT} — {len(state['cookies'])} cookies discogs.com"
              + (" · session détectée ✅" if signed
                 else " · ⚠️ pas de cookie de session — tu n'étais peut-être pas connecté"))
        ctx.close()
        b.close()


if __name__ == "__main__":
    sys.exit(main())
