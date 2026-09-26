# Phase 5 — Base de clôture, mesurée en conditions réelles

Date : 25/09/2026. Portée : [`plans/delegation-multimodel-v1.md`](../../plans/delegation-multimodel-v1.md) §12
(« Phase 5 — Interfaces et mise en service »). Ce document **ne remplace pas** la
base de mesure de la Phase 0
([`phase0-base-de-mesure-2026-09-25.md`](phase0-base-de-mesure-2026-09-25.md:1)) :
il la met à jour à la fin des Phases 3 et 4 et fixe les valeurs à ne pas dégrader.

Toutes les valeurs ci-dessous sont **relevées**, jamais estimées. Les mesures
hors ligne ont été rejouées après les dernières modifications ; les mesures en
ligne viennent d'appels réellement partis du VPS, reçus à l'appui.

## 1. Les quatre références hors ligne — rejouées, tenues

Rejouées après les Phases 3 et 4, sans régression :

| Référence | Valeur mesurée | Détail |
|---|---|---|
| Suite de tests | **387 tests, 4 échecs, 9 erreurs**, 36,957 s | mêmes 4 échecs et 9 erreurs qu'en Phase 0, tous connus : 9 × `ModuleNotFoundError: No module named 'fastapi'` (environnemental), 4 × indices d'authentification de `ai_query` |
| Banc de la passerelle | **54/54** | `adversarial` 39/39 `(discriminants)` + `floor` 15/15 `(non discriminant)` |
| Banc `loop` | **39/39 · 15/15** | `scripts/loop/bench.py ai_broker` : identique au banc direct — le harnais lit le registre sans polluer le journal |
| Manifeste de contexte | **7 documents** | `docs/context-manifest.json` valide, 7 entrées |

La suite est passée de 318 à **387 tests** depuis la Phase 0 (+69) : ce sont les
bancs et tests ajoutés en Phases 1 à 4 (passerelle, courtier, ouvrier, contexte,
délégation). Le **nombre d'échecs et d'erreurs est identique** (4 et 9) : aucune
régression n'a été introduite, et les 13 rouges restants sont ceux du socle
d'origine, pas des ajouts.

Le banc de la passerelle historique, `scripts/bench_ai_query.py --json`, reste à
**13/19** (non rejoué ici) — c'est la mesure qui motivait le remplacement de la
cascade, pas un indicateur du nouveau socle.

## 2. Un appel réel par la passerelle, gratuit, tracé

Un appel réel a été passé sur le seul chemin disponible sans clé (Gemini), en
`--mode summary` :

```
provider        gemini
model           gemma-4-26b-a4b-it
status          ok
paid            False
cost_usd        0.0
jetons          97 (entrée) + 25 (sortie) = 675 (total)
tentatives      1
replis          0
key_id          GEMINI_API_KEY
elapsed_ms      13000
routing_reason  "gratuit retenu"
source          ai_broker
```

Le reçu porte en plus un sous-objet `quota` :

```
{"window": "daily", "limit": null, "used": 13, "remaining": null,
 "paid_spent_usd": 0.0, "paid_cap_usd": 0.0, "paid_remaining_usd": 0.0}
```

Le ledger compte **13 lignes**, dernière ligne au même horodatage que le reçu
(écart < 1 s) — la mesure des « appels partis » et le reçu du tableau de bord
sont bien deux vues du même fait, pas deux compteurs divergents.

Le disjoncteur est vierge : `modeles en cooldown : []`, `modeles retires : []`.

**Le garde-fou payant tient, mesuré et non supposé** :
`paid_cap_usd = 0.0` et `paid_spent = 0.0`. Aucune dépense payante n'est possible
tant que `RADAR_AI_DAILY_PAID_CAP_USD` n'est pas relevé explicitement par
l'opérateur. C'est exactement le défaut sûr demandé au §7 point 5 de la base de
mesure de la Phase 0.

## 3. Un chantier réel de bout en bout par l'ouvrier

Un chantier borné a été exécuté par
[`scripts/ai_worker.py`](../../scripts/ai_worker.py:888) sur un worktree dédié,
avec un rapport machine (`--json`) :

```
statut              termine
arret               null            (aucune borne atteinte)
worktree            .worktrees/worker-cree-le-fichier-scripts-ai-version-socle
branche             worker/cree-le-fichier-scripts-ai-version-socle
etapes              6
jetons_estimes      3992
duree_s             40.153
paye                false
tentatives          ["lister", "lister", "lister", "ecrire", "commande", "terminer"]
commandes           [{"python3 -m py_compile scripts/ai/version_socle.py", code: 0}]
passent             ["python3 -m py_compile scripts/ai/version_socle.py"]
restent_rouges      []
fichiers_modifies   ["scripts/ai/version_socle.py"]
refus               []
erreurs             []
statut_git          " A scripts/ai/version_socle.py"
```

Le chantier a donc été mené de la tâche au diff, dans un worktree, avec un
`py_compile` vert comme porte, **sans commit ni push** — et sans franchir le
périmètre : `refus` et `erreurs` restent vides, aucune commande hors liste
blanche n'a été tentée. La sequence d'outils (`lister` × 3, puis `ecrire`,
`commande`, `terminer`) montre l'exploration avant écriture, comme prévu au
contrat d'outils.

### Deux écarts de qualité observés, consignés tels quels

1. **Le mode `summary` laisse fuiter le raisonnement du modèle** avec le petit
   modèle gratuit courant (`gemma-4-26b-a4b-it`) : la réponse contenait les
   marqueurs de délibération (`*Option A (Empty):*`, `*Draft:*`,
   `*Refining for "Terminal Compressor" style:*`) avant la réponse finale,
   elle-même répétée deux fois. Le prompt de `summary` est orienté journal
   (« n'isoler que les anomalies, les requêtes lentes et les compteurs clés ») :
   à resserrer, ou à réserver à un modèle qui ne délibère pas en clair.
2. **Fidélité de la consigne** : la tâche demandait « une constante
   `VERSION_SOCLE` valant 1.0 » ; l'ouvrier a écrit `VERSION_SOCLE = 1.0` en
   **flottant**, ce qui est correct pour la compilation mais reste une
   interprétation. Écart mineur, sans conséquence ici, mais c'est le genre de
   détail qu'un modèle d'écriture free laisse passer.

Sur le plan du contrat, le reçu porte désormais **à la fois** les champs plats
(`prompt_tokens`, `output_tokens`, `total_tokens`) **et** un sous-objet `tokens` :
les deux sont renseignés, aucune régression pour les lecteurs qui lisaient déjà
les champs plats.

## 4. Le déploiement n'est pas fait, et voici pourquoi

C'est la seule partie de la Phase 5 qui reste ouverte, et elle l'est
volontairement.

**Ce qui a été constaté, en production :**

| Fait | Valeur |
|---|---|
| Conteneurs servis | `radar-radar-ops-1`, `radar-radar-web-1`, `radar-radar-worker-1` (`radar-web:latest`, 17 h), `radar-caddy-1` |
| Dépôt déployé | `/home/ubuntu/radar`, `main` à `7596413` (#220), distant `github.com:alixclaudel-hue/radar.git` |
| `radar_ops/delegation.py` déployé | 10 650 octets, 24/09 17:38 (`md5 4883219b`) |
| `radar_ops/delegation.py` atelier | 20 578 octets, 25/09 21:39 (`md5 87ab6778`) |
| Page `/delegation` servie | **aucune** occurrence de « Occasion » → la Phase 4 n'est pas en ligne |

L'atelier est **deux commits en retard** sur le dépôt déployé (`db18ae5` est un
ancêtre de `7596413`), mais ce retard n'est pas le vrai obstacle.

**Le vrai obstacle :** les deux commits d'avance (#219, #220) ne touchent
**qu'un seul fichier**, `.claude/skills/vps-ops/SKILL.md`. Or la copie de ce
fichier dans l'atelier **n'est pas** `main` + mes ajouts de Phase 5 :

| Mesure | `main` (#220) | Atelier |
|---|---|---|
| Lignes | 499 | 535 |
| Lignes de `main` absentes de l'atelier | — | 51 |
| Lignes de l'atelier absentes de `main` | 87 | — |
| Déclencheurs | « n'importe quel prompt … contient VPS » | liste longue (« diagnostic VPS », « vérifie en prod », …) |
| Rôles | « coder, ouvrir la PR, attendre la CI, merger » | « le rôle que tenait la session de dev cloud avant sa fermeture » + historique |
| Outillage cité | `scripts/ai_query.py`, `ask-gemini` | `scripts/ai_broker.py`, skill `delegate` |

L'atelier a donc **conservé** la version longue que l'utilisateur venait de
condenser en #219/#220, **et** porte mes ajouts de Phase 5. Une PR qui inclurait
ce fichier tel quel **annulerait partiellement** la condensation décidée par
l'utilisateur. C'est un conflit de contenu, pas un simple retard de branche.

**Et le contrat interdit de trancher seul.** Le skill
[`.claude/skills/vps-ops/SKILL.md`](../../.claude/skills/vps-ops/SKILL.md:344)
est explicite : « Le merge reste une décision humaine — exécutée par toi, jamais
décidée par toi … attends une instruction explicite et univoque, donnée pour
CETTE PR précise, avant d'exécuter `gh pr merge` », et « Merger sur `main`
déclenche le déploiement en production (`deploy.yml`) ». Aucune PR n'a donc été
ouverte, aucun commit poussé.

Les trois routes possibles, à trancher par l'utilisateur :

1. **Recaler mes ajouts de Phase 5 sur la version `main`** du fichier (garder la
   condensation #219/#220, n'ajouter que les paragraphes manquants) → PR propre,
   aucun revert ;
2. **Garder la version atelier** (assumer la reprise des passages condensés) →
   PR qui revient en arrière sur #219/#220, à ne faire que si c'est voulu ;
3. **Laisser la PR à l'utilisateur** (je fournis la liste exacte des fichiers et
   des diffs, sans toucher au dépôt).

## 5. Ce qui n'est pas vérifiable à ce stade

- **Les paliers des fournisseurs payants** (OpenRouter 1000 req/jour, DeepSeek,
  xAI) : aucune clé de ces trois fournisseurs n'est présente sur le VPS.
  `GEMINI_API_KEY` est la seule clé posée (`dotenv`, 53 caractères) ; toutes les
  candidatures OpenRouter sont écartées par le routeur avec la raison « aucune
  clé présente », et seuls les 7 modèles Gemini gratuits sont inclus. Aucun
  chiffre de palier n'est donc avancé ici — il sera relevé à l'intégration, comme
  la Phase 0 l'avait déjà posé.
- **La consommation réelle de jetons côté Claude** : non observable depuis le
  VPS. La seule mesure honnête reste les jetons **rapportés** par les API de
  délégation (ici 675 pour l'appel `summary`), jamais une économie estimée.
- **L'accès iPhone réel** : la procédure est documentée
  ([`docs/acces-mobile-et-interfaces.md`](../acces-mobile-et-interfaces.md:1))
  et les faits réseau sont relevés (Tailscale `100.94.157.91`, `radar-ops` sur
  `8610`, jamais l'IP publique), mais l'attache depuis un vrai téléphone reste à
  faire par l'utilisateur.

## 6. Base de clôture — les valeurs à ne pas dégrader

```
suite de tests        387 tests / 4 échecs / 9 erreurs   (36,957 s)
banc passerelle       54/54   (adversarial 39/39, floor 15/15)
banc loop             ai_broker 39/39 · 15/15
manifeste de contexte 7 documents
plafond payant        0.0 USD          (RADAR_AI_DAILY_PAID_CAP_USD vide)
dépense payante       0.0 USD
cooldowns             aucun
modèles retirés       aucun
```

Tout chantier postérieur doit rejouer ces six valeurs avant et après, et
**aucune** ne doit baisser. Toute baisse est une régression, au même titre
qu'un test rouge.
