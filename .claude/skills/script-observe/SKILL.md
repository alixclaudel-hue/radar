---
name: script-observe
description: >
  Collecte un lot d'observations de production pour un script de Radar, sur le
  VPS, et le pousse sur le dépôt `radar-diag`. C'est la jambe « observer » de la
  boucle d'amélioration des scripts ; l'autre jambe, `script-loop`, diagnostique
  et corrige — elle peut être menée par la session VPS elle-même (dans le clone
  séparé ~/radar-work) ou par la session cloud. À utiliser
  quand on demande de collecter, observer, relever les échecs, ou alimenter la
  boucle pour un script — par exemple « /script-observe ytcache ». Réservé à une
  session qui tourne RÉELLEMENT sur le VPS : elle seule voit les journaux, les
  jobs et les données réelles. Ne s'applique jamais à la session de dev cloud.
---

# Collecter un lot d'observations (VPS)

Le contrat `vps-ops` s'applique intégralement et **ce skill n'y fait aucune
exception** : tout reste en lecture seule sur `~/radar`, rien n'est écrit ni
commité dans ce dépôt. Tout ce que tu produis va dans `~/radar-diag`.

Lis `.claude/skills/vps-ops/SKILL.md` d'abord si tu ne l'as pas déjà en mémoire.

## Ce que tu collectes, et ce que tu ne collectes pas

**Les ÉCHECS, pas les succès.** C'est le point qui a coûté un cycle entier le
19/09 : un lot de 40 succès rejoués s'est révélé incapable de distinguer un bon
matching d'un matching entièrement court-circuité. Un succès ne prouve rien — la
recherche externe faisait déjà le travail. Seul un échec discrimine.

Trois familles à chercher, dans cet ordre d'intérêt :

1. **Faux négatifs** — le script n'a rien produit alors qu'une réponse existe.
   Pour `ytcache` : les candidats rejetés au journal de `publish_recos` avec
   « meilleur score X < 0.5 » ou « aucune vidéo trouvée », alors que la piste se
   trouve à la main en deux clics.
2. **Faux positifs** — le script a produit une réponse, mais la mauvaise.
   Pour `ytcache` : les pistes marquées 👎 dans `reco_feedback.json`.
3. **Erreurs franches** — exceptions, abandons de run, compteurs anormaux.

## Étape 0 — cadre

Lis `scripts/loop/registry.json` (dans `~/radar`, en lecture) : l'entrée du
script te donne sa source d'observations (`observe.source`), ce qu'il faut
chercher (`observe.what`) et les charges utiles à capturer pour permettre un
rejeu hors-ligne (`observe.replay_payloads`).

Si le script n'y figure pas, arrête-toi : il n'a pas encore de banc, donc rien
ne pourra mesurer un correctif. Dis-le plutôt que de collecter dans le vide.

## Étape 1 — garde-fous avant de collecter

- Aucun job lourd en cours (`*.status.json`) — surtout si la collecte consomme
  le même quota qu'un job de production.
- Si la collecte fait de vrais appels API (cas de `ytcache` : recapturer les
  réponses `/search` + `/videos` d'un échec coûte ~101 unités de quota Google),
  vérifie le budget restant (`recos_search_budget.json`) et **plafonne**. Un lot
  de 10 à 20 échecs suffit largement à diagnostiquer ; épuiser le quota du jour
  pour collecter casserait la production que tu observes.

## Étape 2 — écrire le lot

Dans `~/radar-diag/observations/<script>/<AAAA-MM-DDTHHMMSSZ>/` :

**`meta.json`**
```json
{
  "script": "ytcache",
  "collected_at": "2026-09-20T02:14:07Z",
  "deployed_sha": "<sha court réellement servi dans le conteneur>",
  "period": "les 7 derniers jours de journaux publish_recos",
  "n_failures": 14,
  "collector": "session VPS"
}
```

**`failures.jsonl`** — une ligne JSON par échec. C'est le fichier qui compte.
Chaque ligne doit contenir de quoi **rejouer l'échec hors-ligne, sans réseau** :
l'entrée exacte, ce qui a été obtenu, la raison donnée par le code, et les
charges utiles brutes listées dans `observe.replay_payloads`.

```json
{"kind": "faux_negatif",
 "input": {"artist": "Alva", "title": "Back In The Studio", "label": ""},
 "got": null,
 "reason": "meilleur score 0.31 < 0.5 : « Alva - Back In The Studio [Hot Haus] »",
 "expected_hint": "https://www.youtube.com/watch?v=... (trouvé à la main)",
 "payloads": {"/search": { }, "/videos": { }}}
```

- `expected_hint` : ce que TU as vérifié à la main comme étant la bonne réponse.
  Sans lui, la boucle ne peut pas écrire un cas de banc qui affirme
  quelque chose. Si tu n'as pas pu trancher, mets `null` et dis-le dans
  `notes.md` — un échec sans réponse connue reste utile pour compter, pas pour
  tester.
- `payloads` : les réponses API BRUTES, telles que reçues. C'est ce qui permet
  au banc de rejouer gratuitement, indéfiniment.

**`metrics.json`** — le dénominateur, sans lequel on ne sait pas si un échec
est marginal ou dominant.
```json
{"runs": 34, "attempts": 210, "ok": 168, "failed": 42,
 "by_reason": {"score trop bas": 31, "aucun résultat": 8, "quota": 3}}
```

**`notes.md`** — ce que les chiffres ne disent pas : ce qui t'a surpris, ce que
tu n'as pas pu vérifier, une hypothèse. Texte libre, court.

## Étape 3 — pousser et journaliser

```
cd ~/radar-diag
git add observations/<script>/
git commit -m "Observations <script> : N échecs (<période>)"
git push
```

Puis une entrée dans `~/radar-diag/memory/journal/<AAAA-MM>.md` : script,
nombre d'échecs, quota consommé, sha déployé, sha du commit.

**Aucun secret dans le lot.** Les charges utiles d'API contiennent des données
publiques (titres, chaînes, identifiants) — mais relis avant de pousser : jamais
de clé, de token, ni de contenu de `.env`, même partiel. `radar-diag` part sur
GitHub.

## Étape 4 — rapport

Format habituel du contrat `vps-ops` (verdict, actions effectuées, ce qui
reste). Termine par la ligne que la boucle attend :

> Lot prêt : `observations/<script>/<horodatage>/` — N échecs, sha `<sha>`.

## Ce que tu ne fais pas

- **N'écris rien dans `~/radar`**, pas même un fichier de travail. Collecter
  est une opération en lecture seule sur le checkout de production.
- **Ne corrige pas le script depuis ce skill.** Collecter et corriger sont deux
  temps distincts : une fois le lot poussé, enchaîne avec `/script-loop
  <script>`, qui travaille dans `~/radar-work` (clone séparé) et suit ses
  propres garde-fous. Passer directement du journal au correctif, sans cas de
  banc qui échoue d'abord, c'est exactement ce que le process interdit.
- **Ne collecte pas des succès pour faire du volume.** Un lot de 5 vrais échecs
  vaut mieux que 50 succès : les succès ne mesurent rien (cf. en-tête).
