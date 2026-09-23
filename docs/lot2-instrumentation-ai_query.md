# Lot 2 — instrumentation de `scripts/ai_query.py` (à réappliquer)

Ce document existe pour une raison précise : les modifications décrites ci-dessous
ont été **écrites et vérifiées en conditions réelles** (appel Gemini abouti, reçu
enrichi produit), mais la session qui les a faites a perdu l'accès au shell avant
de pouvoir les committer. Retranscrire les 528 lignes du fichier à travers l'API
GitHub aurait fait courir un risque de coquille sur le script qui conditionne
**toute** délégation et le hook de commit — inacceptable pour un gain de confort.

Les six modifications sont donc consignées ici mot pour mot. Les réappliquer prend
quelques minutes et se vérifie par un appel réel (`--mode summary`) suivi de la
lecture de la dernière ligne de `.claude/gemini-receipts.jsonl`.

## Pourquoi

Le reçu écrit à chaque appel ne contenait que `{ts, mode, status}` — aucun
compteur. Le tableau de bord de délégation de `radar_ops` doit afficher le volume
**réellement consommé** par Gemini (décision utilisateur : jetons Gemini mesurés,
pas d'estimation d'économie côté Claude). Ce volume est rapporté par l'API dans
`usageMetadata` ; il suffisait de le faire remonter jusqu'au reçu.

Le hook `scripts/hooks/gemini_gate.py` ne lit toujours que `ts`, `mode` et
`status` : un reçu ancien, sans les nouveaux champs, reste valide.

## 1. `query_gemini` renvoie aussi la consommation

Signature : `-> str` devient `-> tuple[str, dict]`, et la docstring gagne :

```
    """Envoie une requête à l'API Gemini, renvoie `(texte généré, jetons consommés)`.

    La consommation vient de `usageMetadata`, que l'API rapporte elle-même : le
    tableau de bord de délégation affiche une mesure, jamais une estimation.
```

Corps, dans le bloc `try` :

```python
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            usage = _usage(result)
            candidates = result.get("candidates", [])
            if not candidates:
                return "", usage
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(part.get("text", "") for part in parts).strip(), usage
```

## 2. `query_with_fallback` propage la mesure et compte les replis

Signature : `-> tuple[str, str]` devient `-> tuple[str, str, dict]`. Docstring :

```
    Renvoie `(réponse, modèle réellement utilisé, consommation de jetons)`.
    La consommation est celle du SEUL appel qui a abouti : un modèle indisponible
    répond en erreur sans rien facturer, le compter fausserait la mesure.
```

Branche « modèle explicite » :

```python
    if explicit_model:
        text, usage = query_gemini(
            prompt=prompt,
            system_instruction=system_instruction,
            model=explicit_model,
            api_key=api_key,
            temperature=temperature,
            timeout=timeout,
        )
        return text, explicit_model, usage
```

Boucle de cascade : initialiser `fallbacks = 0` à côté de `last_error`, puis

```python
            text, usage = query_gemini(
                prompt=prompt,
                system_instruction=system_instruction,
                model=model,
                api_key=api_key,
                temperature=temperature,
                timeout=timeout,
            )
            usage["fallbacks"] = fallbacks
            return text, model, usage
```

et incrémenter `fallbacks += 1` juste après `last_error = err`.

## 3. Nouvelle fonction `_usage`, juste avant `write_receipt`

```python
def _usage(result: dict) -> dict:
    """Consommation de jetons telle que l'API la RAPPORTE (`usageMetadata`).

    Mesure, jamais estimation : c'est ce que `radar_ops` affiche comme volume
    réellement délégué à Gemini. Un champ absent vaut 0 plutôt que None — une
    somme sur une colonne ne doit pas dépendre de la complétude de la réponse.
    """
    u = result.get("usageMetadata") or {}
    return {
        "prompt_tokens": int(u.get("promptTokenCount") or 0),
        "output_tokens": int(u.get("candidatesTokenCount") or 0),
        # `totalTokenCount` inclut aussi les jetons de raisonnement, absents des
        # deux autres compteurs : on le garde tel quel plutôt que de le recalculer.
        "total_tokens": int(u.get("totalTokenCount") or 0),
    }
```

## 4. `write_receipt` accepte des champs supplémentaires

Signature : `def write_receipt(mode: str, status: str, **extra) -> None:`.
Ajout à la docstring :

```
    `extra` porte la mesure lue dans `usageMetadata` (modèle, tier, jetons,
    durée) : c'est la source du tableau de bord de délégation de `radar_ops`.
    Le hook, lui, ne lit toujours que `ts`, `mode` et `status` — un reçu ancien
    sans ces champs reste valide.
```

Corps : la construction de la ligne devient

```python
        row = {"ts": time.time(), "mode": mode, "status": status}
        row.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(row, ensure_ascii=False)
```

Le filtre sur `None` évite d'écrire une colonne vide quand `CLAUDE_SESSION_ID`
n'est pas défini.

## 5. `main()` mesure de bout en bout

Remplacer le bloc d'appel par :

```python
    started = time.time()
    # Taille de la demande, connue même quand l'appel échoue : c'est le volume
    # que Claude n'a pas eu à ingérer, et donc l'information utile d'un échec.
    meta = {"tier": chosen_tier, "prompt_chars": len(full_prompt),
            "session": os.environ.get("CLAUDE_SESSION_ID")}

    try:
        response, used_model, usage = query_with_fallback(
            prompt=full_prompt,
            system_instruction=sys_instruction,
            tier=chosen_tier,
            explicit_model=args.model,
        )
    except Exception as e:
        sys.stderr.write(f"Erreur : {e}\n")
        write_receipt(args.mode, "error",
                      elapsed_ms=round((time.time() - started) * 1000), **meta)
        return 1

    meta.update(usage, model=used_model,
                elapsed_ms=round((time.time() - started) * 1000))
```

## 6. Les trois autres `write_receipt` reçoivent `meta`

- réponse vide : `write_receipt(args.mode, "error", **meta)`
- échec de `--check-syntax` : `write_receipt(args.mode, "error", **meta)`
- succès : `write_receipt(args.mode, "ok", output=args.output, **meta)`

## Vérification attendue

```
python3 scripts/ai_query.py --mode summary "une phrase de test"
tail -1 .claude/gemini-receipts.jsonl
```

La dernière ligne doit ressembler à celle réellement obtenue lors de la mise au
point :

```json
{"ts": 1790147049.81, "mode": "summary", "status": "ok", "tier": "fast",
 "prompt_chars": 78, "prompt_tokens": 84, "output_tokens": 14,
 "total_tokens": 98, "fallbacks": 0, "model": "gemini-3.5-flash-lite",
 "elapsed_ms": 1194}
```

## Reste du lot 2, non commencé

- récupérateur côté VPS (`scripts/telemetry_pull.sh`) : `git fetch origin telemetry`
  depuis un clone séparé, puis report des nouvelles lignes dans `/data/ops/`, en
  n'ajoutant que les événements dont l'horodatage dépasse le maximum déjà stocké ;
- page tableau de bord de délégation dans `radar_ops` (jetons par mode, par modèle,
  taux de repli de cascade, part des tâches déléguées) ;
- tests de `scripts/hooks/telemetry.py` et `scripts/telemetry_ship.py` ;
- ajout de `.claude/telemetry.jsonl` à `.gitignore` ;
- branchement des hooks dans `.claude/settings.json`
  (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`).
