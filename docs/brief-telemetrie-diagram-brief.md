# Diagram Brief: chaîne de télémétrie et de délégation (lot 2 `radar_ops`)

**Layout**: left-to-right
**Flow summary**: Montre comment une session de développement produit deux journaux
(événements de session et reçus d'appels Gemini), comment ces journaux traversent une
branche git servant de boîte aux lettres, et comment le VPS les verse dans le dossier
que lit la page Délégation de `radar_ops`.

Source : `docs/brief-telemetrie.md`. Les directives de dessin restent en anglais
(`Create a box labeled`, `Draw an arrow from`) parce que le rendu Excalidraw les
reconnaît à ces formules exactes ; les libellés et les notes sont en français.

---

## Elements

Create a group labeled "Session de développement (cloud ou VPS)" that contains: Claude Code, Hook telemetry.py, telemetry.jsonl, ai_query.py, gemini-receipts.jsonl, Garde gemini_gate.py.

Create a group labeled "Transport git" that contains: telemetry_ship.py, Branche telemetry.

Create a group labeled "VPS de production" that contains: telemetry_pull.py, Dossier /data/ops.

Create a group labeled "radar_ops (port 8610)" that contains: delegation.py, Page /delegation.

Create a box labeled "Claude Code". Note: le harnais, point d'entrée — déclenche les hooks sur SessionStart, UserPromptSubmit, PostToolUse, Stop et SessionEnd.

Create a box labeled "Hook telemetry.py". Note: capture un événement par ligne. Ne journalise jamais une commande Bash, seulement sa description.

Create a box labeled "telemetry.jsonl". Note: fichier JSONL, rotation à 5 Mo. Emplacement réglé par RADAR_TELEMETRY_DIR.

Create a box labeled "ai_query.py". Note: passerelle de délégation. Cascade de modèles avec repli sur 429, 404 et 5xx.

Create a box labeled "API Gemini". Note: service externe. Rapporte lui-même sa consommation dans usageMetadata.

Create a box labeled "gemini-receipts.jsonl". Note: un reçu par appel — mode, statut, tier, modèle, replis, jetons, durée.

Create a box labeled "Garde gemini_gate.py". Note: hook PreToolUse. Bloque commit, test et nouveau module tant qu'aucun reçu de moins d'une heure n'existe.

Create a box labeled "telemetry_ship.py". Note: écrit les objets git directement, sans toucher à l'index ni à la branche courante. Lancé à la demande, jamais par un hook.

Create a box labeled "Branche telemetry". Note: boîte aux lettres, pas un historique — commit sans parent poussé en force, les 2000 dernières lignes de chaque journal.

Create a box labeled "telemetry_pull.py". Note: fetch d'une référence dédiée, puis fusion des seules lignes dont le ts dépasse le maximum déjà stocké.

Create a box labeled "Dossier /data/ops". Note: volume partagé. Écriture par fichier temporaire puis os.replace, parce que radar_ops y lit pendant qu'on y écrit.

Create a box labeled "delegation.py". Note: agrégation en lecture seule — totaux, par mode, par modèle, par jour, part déléguée, sessions.

Create a box labeled "Page /delegation". Note: réservée au propriétaire. Nomme les fichiers manquants plutôt que d'afficher des zéros sans explication.

Draw an arrow from Claude Code to Hook telemetry.py. Label it "événement de session".

Draw an arrow from Hook telemetry.py to telemetry.jsonl. Label it "une ligne JSON".

Draw an arrow from Claude Code to ai_query.py. Label it "tâche déléguée".

Draw an arrow from ai_query.py to API Gemini. Label it "requête, tier heavy ou fast".

Draw an arrow from API Gemini to ai_query.py. Label it "réponse + usageMetadata".

Draw an arrow from ai_query.py to gemini-receipts.jsonl. Label it "reçu mesuré".

Draw an arrow from gemini-receipts.jsonl to Garde gemini_gate.py. Label it "preuve de tentative".

Draw an arrow from Garde gemini_gate.py to Claude Code. Label it "autorise ou bloque l'action".

Draw an arrow from telemetry.jsonl to telemetry_ship.py. Label it "2000 dernières lignes".

Draw an arrow from gemini-receipts.jsonl to telemetry_ship.py. Label it "2000 dernières lignes".

Draw an arrow from telemetry_ship.py to Branche telemetry. Label it "commit sans parent, poussé en force".

Draw an arrow from Branche telemetry to telemetry_pull.py. Label it "fetch de refs/remotes/origin/telemetry".

Draw an arrow from telemetry_pull.py to Dossier /data/ops. Label it "fusion incrémentale par ts".

Draw an arrow from Hook telemetry.py to Dossier /data/ops. Label it "session VPS : écriture directe, sans git".

Draw an arrow from Dossier /data/ops to delegation.py. Label it "lecture seule".

Draw an arrow from delegation.py to Page /delegation. Label it "agrégat".

Mark Claude Code as entry point.

Mark API Gemini as external.

Mark telemetry.jsonl as database.

Mark gemini-receipts.jsonl as database.

Mark Branche telemetry as database.

Mark Dossier /data/ops as database.

Mark Page /delegation as entry point.

---

## Notes

- La flèche « session VPS : écriture directe » est le raccourci du contrat : une
  session qui tourne SUR le VPS écrit dans `/data/ops` et court-circuite les trois
  boîtes du transport git. C'est le seul chemin qui saute des étapes, d'où son tracé
  distinct.

- `opslog.py` et `actions.jsonl` alimentent la page Actions de `radar_ops` par un
  chemin parallèle (écriture par l'application web, lecture par le tableau de bord).
  Volontairement hors de ce schéma : ils décrivent l'usage de l'application, pas la
  délégation. À ajouter si le lot 3 veut une vue unique des quatre pages.

- La boucle de contrôle du schéma est le couple `gemini-receipts.jsonl` →
  `gemini_gate.py` → `Claude Code` : c'est elle qui rend la règle de délégation
  contraignante au lieu de déclarative. Si le rendu permet un style distinct, la
  tracer en boucle fermée plutôt qu'en flèches droites.

- Le tampon `Branche telemetry` est délibérément une boîte et non une simple flèche :
  son écrasement à chaque envoi, et le recouvrement des expéditions qu'il impose, sont
  ce qui justifie la fusion par `ts` en aval.
