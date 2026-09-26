# Phase 0 — Base de mesure de la délégation Gemini

Date : 25/09/2026. Portée : [`plans/delegation-multimodel-v1.md`](../../plans/delegation-multimodel-v1.md) §12
(« Phase 0 — Mesurer et recenser »). Aucune ligne de code produit.

Toutes les valeurs de ce document sont **relevées**, jamais estimées : elles
viennent du journal de reçus `gemini-receipts.jsonl` et d'exécutions réelles
commandées sur le VPS.

## 1. Méthode, et pourquoi le premier chiffre était faux

Trois fichiers de reçus coexistent sur le VPS :

| Fichier | Lignes | Rôle |
|---|---|---|
| `~/radar/data/ops/gemini-receipts.jsonl` | 1069 | journal partagé, lu par `radar_ops` (volume `/data`) |
| `~/radar-work/.claude/gemini-receipts.jsonl` | 529 | reçus du checkout de travail |
| `~/radar/.claude/gemini-receipts.jsonl` | 71 | reçus du checkout de production |

Le recouvrement a été vérifié ligne à ligne : `data/ops` est un **sur-ensemble
exact** des deux autres (`data/ops ∩ work` = 529 = work entier, `data/ops ∩ radar`
= 71 = radar entier). Union = 1069, soit **600 doublons** si l'on additionne les
trois fichiers. La source unique retenue est donc `data/ops` — c'est aussi le
seul fichier que `radar_ops` peut lire.

Une première lecture, toutes lignes confondues, donnait 463 échecs sur 1669
appels, soit « 27,7 % d'échec ». Ce chiffre était **gonflé par deux artefacts**
que la suite de l'analyse a isolés :

1. le comptage en double (600 lignes) ;
2. un bruit massif dans le journal lui-même (voir §3).

## 2. Base de mesure honnête — 88 appels réels

Critère retenu : une ligne dont `elapsed_ms > 0` correspond à un appel réseau
réellement tenté. Les autres (`elapsed_ms == 0`, `prompt_chars` constant à
quelques caractères) sont des lignes synthétiques produites par le harnais de
tests avant son isolement (§3).

```
appels réels                       88
volume délégué                     1 143 324 caractères  (~285 831 jetons)
durée cumulée                      2 144,3 s
échecs                             37  (42,0 %)
repli de cascade                   22 appels (25,0 %), dont 12 ont épuisé
                                   la cascade entière (fallbacks = 5)
```

Répartition par mode :

| Mode | Appels | Volume (car.) | Échecs |
|---|---|---|---|
| `general` | 32 | 222 472 | 21 (65,6 %) |
| `pr` | 26 | 377 836 | 8 (30,8 %) |
| `code` | 15 | 170 588 | 5 (33,3 %) |
| `test` | 12 | 369 111 | 3 (25,0 %) |
| `summary` | 2 | 96 | 0 |
| `diag` | 1 | 3 221 | 0 |

Causes d'échec (37) : `error_type` absent 14, `cascade_exhausted` 12,
`http_403` 5, `api_error` 2, `http_404` 1, `http_503` 1, `http_429` 1,
`syntax_error` 1. **Quatorze échecs sur trente-sept ne portent aucune cause
exploitable** : c'est le finding n°1 du diagnostic
[`ai_query-2026-09-23.md`](ai_query-2026-09-23.md:5), toujours ouvert.

Modèle ayant effectivement répondu : `gemini-3.5-flash-lite` 41, `gemini-3.6-flash`
6, `gemini-3.8-flash` 3, `gemini-3.7-flash` 1. **`gemini-3.1-pro-preview` n'a
jamais répondu** — il est en 4ᵉ position d'une cascade de six.

## 3. Le journal est inexploitable en l'état

- **879 lignes sur 1069 (82 %) ne sont pas des appels de production.** Le bruit
  se décompose en 432 lignes `elapsed_ms == 0` **et** `prompt_chars` minuscule,
  produites en 50 rafales de 10 lignes strictement identiques
  (`code:7, general:2, diag:1`), toutes datées du 2026-09-23, plus des lignes de
  même signature réparties dans les deux autres fichiers.
- **Zéro reçu après le correctif `f1fd151`** (« Isole MainTests du fichier de
  reçus Gemini partagé », 24/09 20:40:35). Autrement dit : le journal n'enregistre
  plus rien depuis, et tout ce qu'il contient est antérieur. Une hypothèse
  « la suite de tests pollue le journal de production » a été testée par une
  mesure avant/après explicite : **delta = 0 sur les deux journaux**. L'hypothèse
  est donc fausse pour le code actuel ; elle l'était pour le code d'avant.
- Conséquence directe : la page `delegation` de `radar_ops`
  ([`radar_ops/delegation.py`](../../radar_ops/delegation.py:215)) affiche
  aujourd'hui principalement du bruit, et ne mesure rien de la période courante.

**Décision proposée** : archiver le journal historique, purger les lignes
`elapsed_ms == 0`, et estamper chaque nouveau reçu d'un champ `source`
(`production` / `bench` / `test`) pour que cette contamination soit
structurellement impossible, pas seulement corrigée une fois.

## 4. Le mode `read` n'a jamais été appelé en production

21 reçus de mode `read` existent : **tous** ont `elapsed_ms == 0` et un
`prompt_chars` constant de 70. Aucun n'est un appel réel. Le constat de
l'utilisateur (« le mode read n'est jamais appelé ») est donc confirmé
mécaniquement, sans interprétation : ce mode existe
([`scripts/ai_query.py`](../../scripts/ai_query.py:135)) et n'est jamais
déclenché, parce que rien ne l'impose. C'est exactement ce que règle la refonte
`context` + `SessionStart` du plan §8.

## 5. État rouge de référence (à ne pas dégrader)

| Mesure | Valeur | Détail |
|---|---|---|
| Suite de tests | 318 tests, **4 échecs, 9 erreurs**, 36,8 s | 9 erreurs = `ModuleNotFoundError: No module named 'fastapi'` (`radar_web`, environnemental) ; 4 échecs = indices d'authentification de `ai_query` (`AuthHintTests.test_400_sur_cle_invalide_avec_indice`, `AuthHintTests.test_403_avec_indice`, `QueryGeminiTests.test_sans_cle_locale_url_ne_contient_pas_key`, `QueryGeminiTests.test_sans_cle_ni_identifiant_reseau_erreur_gemini_explicite`) |
| Banc de la passerelle | **13/19** | Les **6 échecs sont tous des cas de résilience de cascade** : `cascade_epuisee`, `cascade_429_quota`, `cascade_429_ratelimit`, `cascade_500_interne`, `cascade_502_passerelle`, `cascade_timeout`. `adversarial` 10/11, `floor` 3/8 |
| Banc `loop` | identique au banc direct | `scripts/loop/bench.py` ne pollue pas le journal (delta 0) |

La robustesse de la cascade est donc le point le plus faible du socle actuel, et
c'est précisément ce que la passerelle multi-fournisseurs doit remplacer.

## 6. Recensement des accès et des paliers

Réseau sortant du VPS, un appel de test par fournisseur (TLS < 0,2 s pour les
quatre) :

| Fournisseur | Test réseau | Clé présente |
|---|---|---|
| OpenRouter | 200 | **non** |
| DeepSeek | 401 (attendu sans clé) | **non** |
| xAI Grok | 401 (attendu sans clé) | **non** |
| Google Gemini | 403 (attendu sans clé) | **oui** — `GEMINI_API_KEY` |

- `~/radar/.env` déclare 16 variables, dont `GEMINI_API_KEY` ; `~/radar-work/.env`
  n'en déclare qu'une. **Aucune clé OpenRouter, DeepSeek ou xAI n'est présente**
  sur le VPS à ce jour.
- Chemin passerelle cloud : `~/.claude-code-router/` **absent** et
  `/tmp/radar-gemini-gateway.url` **absent**. Les appels partent donc en direct
  vers Google ; le mode « passerelle locale » du code actuel est mort sur ce VPS.
- Ports en écoute : 8610 sur `100.94.157.91` (Tailscale) et `127.0.0.1`, 8600 sur
  `127.0.0.1`. **Aucun port 8632** — le port par défaut de
  [`scripts/gemini_gateway.py`](../../scripts/gemini_gateway.py:190) n'est pas en
  service.
- `ListModels` (v1beta) renvoie **61 modèles** et **tous les noms des deux
  cascades existent** — le catalogue actuel n'est pas cassé par un renommage.

Les paliers réels (1000 req/jour OpenRouter après dépôt du crédit unique, quota
gratuit DeepSeek, volume xAI à l'ouverture du compte) **ne sont pas vérifiables
sans les clés** : ils restent au §11 point 2 du plan, à relever au moment de
l'intégration, jamais supposés depuis le plan.

## 7. Ce que ce constat impose à la Phase 1

1. **Le journal doit redevenir une mesure.** Sans champ `source`, la nouvelle
   passerelle héritera du même problème : le tableau de bord ne pourra pas
   distinguer un appel réel d'une ligne de banc.
2. **Le point faible à traiter en priorité est la queue de cascade**, pas le
   premier maillon : 25 % des appels réels ont consommé un repli, 12 ont brûlé
   six modèles pour rien. Un disjoncteur par modèle et un routage qui écarte les
   modèles morts remplacent avantageusement un sixième nom dans une liste.
3. **Le mode `general` est le plus coûteux et le plus fragile** (65,6 % d'échec,
   ~20 % du volume) : c'est là que le basculement vers des modèles gratuits
   non-Google a le plus de valeur.
4. **`pr` fonctionne** (30,8 % d'échec mais c'est le mode le plus utilisé après
   `general`, et le seul dont la sortie est directement actionnable) : le plan
   §6 le conserve tel quel, décision confirmée par la mesure.
5. **Aucun appel réel ne doit dépendre d'un modèle payant** tant que les clés
   gratuites ne sont pas posées : le défaut sûr est un plafond payant à 0 $,
   relevé explicitement par l'opérateur.

## 8. Ce que je n'ai pas pu trancher

- Les paliers réels des quatre fournisseurs : clés absentes du VPS.
- La forme exacte du champ de raisonnement chez OpenRouter, DeepSeek et xAI :
  elle sera relevée par un appel trivial par fournisseur au moment de
  l'intégration, comme prévu au §11 point 5 du plan.
- La consommation réelle de jetons côté Claude : non observable depuis le VPS.
  La règle du projet reste donc la seule mesurable — compter les jetons
  **rapportés** par les API de délégation, jamais une économie estimée.
