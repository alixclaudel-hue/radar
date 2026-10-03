# Radar-web — Design System v4 « Strong White & Acacia » (spec d'implémentation)

> Référence visuelle : [`docs/styleguide-preview.html`](../docs/styleguide-preview.html) (ouvrir dans un navigateur). **Le CSS de ce fichier fait foi : le copier, ne pas le réinterpréter.**
> Concept : blanc cassé chaud, texte anthracite (jamais noir pur), accents acacia huilé et sauge, bordures fines, ombres douces diffuses. On garde du caractère via Syne 800 pour les titres, Space Mono pour catnos/scores/labels techniques et le logo « disque » (loader vinyle). **Header clair.**

## 1. Constat sur l'existant (résumé)
- Un seul CSS : [`radar_web/static/app.css`](../radar_web/static/app.css) (~475 lignes), 8 variables, valeurs codées en dur. Base : [`radar_web/templates/base.html`](../radar_web/templates/base.html) (topbar + `.nav` pastilles + `<hr>`), htmx 2.0.4 CDN.
- ~97 `style="..."` inline dans les templates ; un seul style de bouton ; cartes/panneaux/notices sans socle commun ; pas d'échelle typo/espacement.
- Direction actuelle « Sleeve » : crème, vert `#1C4A3E`, terracotta, pilules 999px.
- `radar_ops/` est une autre app : **hors périmètre**.

## 2. Règles impératives (pour tout développeur / modèle)
1. **Cosmétique uniquement** : ne jamais modifier routes, `hx-*`, `name`, `id`, `data-*`, variables/blocs Jinja, JS fonctionnel, Python.
2. **Ne jamais renommer ni supprimer une classe existante utilisée par du JS/htmx** : `.combo .lbl-suggest .lbl-opt .chip-list .vchip .vchip-x .wl-matches [data-wl-matches] #cart-list .gnode .gfull .yrange .wradar .fb-* .htmx-indicator .htmx-request .now-playing .tk-play .rm-panel`. On restyle, on ne renomme pas.
3. Garder les alias des anciennes variables (§3) tant que des règles les utilisent.
4. Itératif : **un lot = un commit = une validation visuelle**. Après chaque lot : `.venv/bin/python -m unittest discover -s tests` (le projet n'a pas pytest ; des tests inspectent le HTML rendu).
5. `prefers-reduced-motion` respecté ; contrastes AA ; cibles tactiles ≥ 40px ; le `@media (max-width:640px)` de police 16px sur les champs reste (anti-zoom iOS).
6. Emojis de navigation conservés, icônes SVG de `_icons.html` inchangées.
7. **Pas de noir pur, pas d'ombre dure, pas de couleur saturée.** Ombres uniquement `--shadow-*`.

## 3. Tokens (à coller en tête de `app.css`, remplace `:root`)

```css
:root{
  color-scheme: light;
  /* Fonds */
  --bg:#F5F3EF; --bg-2:#ECE8E1; --bg-sunk:#E2DDD4;
  /* Surfaces */
  --surface:#FBFAF7; --field:#FFFFFF; --surface-hover:#F1EEE8;
  /* Textes */
  --text:#2B2A28; --text-soft:#5E5A54; --text-faint:#8C867D; --on-primary:#FBFAF7;
  /* Primaire : acacia huilé */
  --primary:#8A6A47; --primary-hover:#725636; --primary-soft:#EDE4D6;
  /* Secondaire : sauge + terre cuite */
  --secondary:#6F7A68; --secondary-soft:#E3E7DE; --clay:#B0805A;
  /* États */
  --success:#5F7A5A; --warn-soft:#F1E6D0; --danger:#A3473A; --danger-soft:#F1DDD8;
  /* Bordures */
  --border:#E0DBD2; --border-strong:#C9C2B6; --bandcamp:#1DA0C3;
  /* Espacement / rayons / ombres */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px; --sp-7:56px;
  --r-sm:8px; --r-md:14px; --r-lg:20px; --r-pill:999px;
  --shadow-sm:0 1px 2px rgba(43,42,40,.06);
  --shadow-md:0 6px 18px rgba(43,42,40,.08);
  --shadow-lg:0 20px 48px rgba(43,42,40,.16);
  --font-display:'Syne',sans-serif; --font-body:'Work Sans',system-ui,sans-serif; --font-mono:'Space Mono',ui-monospace,monospace;
  --fs-xs:12px; --fs-sm:13px; --fs-md:15px; --fs-lg:18px; --fs-xl:26px; --fs-2xl:40px;

  /* ALIAS anciens noms (ne pas supprimer avant la fin de l'étape 3) */
  --paper:var(--bg); --ink:var(--text); --soft:var(--text-soft); --line:var(--border);
  --pin:var(--primary); --pin-dark:var(--primary-hover); --radius:var(--r-md);
}
```
Polices : dans `base.html` ajouter `&family=Space+Mono:wght@400;700` à l'URL Google Fonts existante.

### Justification des couleurs
- **Fonds** `#F5F3EF / #ECE8E1 / #E2DDD4` : blanc cassé « Strong White » puis gris chaud plus profonds pour zones secondaires et éléments enfoncés.
- **Surfaces** `#FBFAF7` (cartes, menus, modales) ; `#FFFFFF` réservé aux champs de saisie pour qu'ils ressortent.
- **Textes** `#2B2A28` (≈13:1), `#5E5A54` (≈6:1, AA), `#8C867D` (placeholders uniquement).
- **Primaire** acacia `#8A6A47` (texte clair dessus ≈4,9:1), hover `#725636`, teinte `#EDE4D6` pour les états actifs.
- **Secondaire** sauge `#6F7A68` / `#E3E7DE` ; terre cuite `#B0805A` pour ponctuer (étoiles).
- **États** succès `#5F7A5A`, avertissement `#F1E6D0`, danger `#A3473A` (argile désaturé).
- **Bordures** `#E0DBD2` (fines, quasi fondues), `#C9C2B6` (champs).

## 4. Correspondance composants existants → nouveau style

| Existant | Nouveau rendu (voir preview) |
|---|---|
| `body` | fond `--bg`, texte `--text`, interligne 1.6 |
| `.topbar` + `.nav` + `<hr>` | **Header clair sticky** `.app-header` (fond `--surface` translucide + flou, trait bas `--border`, logo Syne + `.disc`, nav en pilules : actif = `--primary-soft` texte `--primary-hover`, profil à droite avec avatar `--primary`). `<hr>` supprimé. |
| `button`, `.btn` | 42px, rayon 8px, bordure `--border-strong`, fond `--field`, ombre `--shadow-sm`, hover `--surface-hover`. `.primary` = acacia, `.secondary-solid` = sauge, `.ghost`, `.danger` (= `.btn-stop`), `.small` = 32px |
| `input/select/textarea` | 42px, bordure `--border-strong`, focus = bordure `--primary` + halo `0 0 0 3px var(--primary-soft)` |
| `label` | 13px, 500, `--text-soft` |
| `.card` | bordure 1px `--border`, `--shadow-sm`, hover `--shadow-md` + `translateY(-2px)` ; `.cover` avec disque vinyle en `::after` ; score = **pastille ronde `.score`** blanche en haut à droite de la pochette ; `.catno` en mono |
| `.notice`, `.acc`, `.stat`, `.panel` | même socle : bordure 1px, rayon 14px, `--shadow-sm` ; `.notice.warn` sable, `.ok` sauge, `.err` argile |
| `.chip`, `.vchip`, `.badge` | pilules sans bordure : acacia-soft / sauge-soft / sable / neutre ; `.badge` = anthracite + mono |
| onglets `univers` (`.nav` réutilisé) | composant `.tabs` : soulignement 2px acacia sur l'actif |
| `.tbl` | en-tête `--bg-2` mono majuscules, lignes séparées par `--border`, hover `--surface-hover`, pas de zébrage ; `.now-playing` = fond `--primary-soft` + texte `--text` |
| `.wl-matches`, `.fb-composer`, `.gfull__sheet` | modale : rayon 20px, `--shadow-lg`, en-tête séparé par un trait, pied `--bg` |
| `.spinner` | disque vinyle qui tourne (acacia au centre) |
| `.section-title` | h3 ; `.section-label` = mono 12px majuscules `--text-faint` + filet horizontal |
| `<h1>` de page | Syne 800 40px, dans `.page-header` |

Les autres règles du CSS actuel (graphes `.gnode`, `.yrange`, `.wradar`, `.fb-*`…) : conserver la logique, ne remplacer que les couleurs/bordures via les nouveaux tokens.

## 5. Plan de développement (lots, chacun validé visuellement)

**Lot 0 — Fondations** : coller les tokens + alias, ajouter Space Mono, `body`, `h1–h4`, liens, focus visible. Aucun changement HTML.
**Lot 1 — Header/Footer/Layout** : réécrire le haut de `base.html` (`.app-header`, `.disc`, nav, menu profil `<details>` conservé avec ses liens et son formulaire `/logout`), `.wrap` 1180px, footer. Ajouter le bloc Jinja optionnel `{% block page_header %}` **sans casser** les pages existantes. Mobile <720px : nav en défilement horizontal.
**Lot 2 — Boutons, champs, puces, notices** (CSS seul).
**Lot 3 — Cartes, tableaux, onglets, accordéons, stats, modales, loader** (CSS seul).
**Lot 4+ — Pages, une à la fois** dans cet ordre : `search` → `reco_radar` → `univers` → `patte` → `wantlist`/`tracks_aimees` → `veille` → `settings` → `disco` → `feedback`. Pour chaque page : page-header, sections en panneaux aérés, remplacement des `style="…"` inline par des utilitaires (`.stack`, `.row`, `.grow`, `.mt-3`…) ajoutés au CSS ; aucune information supprimée.

## 6. Points de vigilance
- Tests qui lisent le HTML rendu : `tests/test_cart_add_vinyl_fallback.py`, `tests/test_search_seller.py`, etc. → `.venv/bin/python -m unittest discover -s tests` après chaque lot (755 tests, ~40 s).
- Le score des cartes (`.badge`) devient une pastille `.score` : ne changer que la classe/CSS, pas la valeur Jinja.
- `select[multiple]` et `.yrange` : vérifier visuellement après le lot 2.
- Couleurs codées en dur restantes dans `app.css` (`#fff`, `rgba(0,0,0,…)`, `var(--clay)` utilisé pour les erreurs) : les remapper vers `--danger` / tokens.
