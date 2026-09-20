---
name: ask-gemini
description: >
  Délègue à Gemini (gratuit, DEFAULT_MODEL de scripts/ai_query.py) les
  tâches mécaniques qui consomment beaucoup de tokens Claude sans exiger de
  jugement : résumer un journal volumineux, analyser un dump ou une trace
  Docker, générer un premier jet de tests ou de docstrings. Utiliser
  scripts/ai_query.py via Bash quand une tâche de lecture ou de génération
  pure peut être déléguée sans perte de qualité sur le résultat final.
  Authentification via GEMINI_API_KEY (VPS/local) OU un identifiant réseau
  configuré côté environnement (session cloud, cf. Prérequis) — les deux
  absents, le dire et continuer sans délégation plutôt que d'inventer une
  réponse.
---

# Skill : ask-gemini

Délègue une tâche de lecture volumineuse ou de premier jet textuel au modèle
gratuit Gemini (Google AI Studio), pour économiser les tokens Claude.

## Quand l'utiliser ?

- Analyser un journal de logs volumineux (>50 lignes).
- Résumer une table ou un dump SQLite de test.
- Générer une première passe de cas de test unitaires ou de docstrings.
- Détecter rapidement la ligne d'une exception dans une trace Docker.

## Quand NE PAS l'utiliser

- Toute tâche qui touche au code applicatif final, au scoring, ou à une
  décision produit : Gemini peut dégrossir, mais Claude relit et écrit le
  résultat qui part en commit/PR. Ne jamais coller une sortie Gemini
  telle quelle dans un fichier du dépôt sans l'avoir vérifiée.
- Tout contenu sensible : jamais de token, clé, contenu de `.env`, ou
  donnée personnelle dans le prompt envoyé à Gemini (service externe,
  hors du périmètre de confiance du dépôt).

## Prérequis

Deux modes d'authentification possibles, `scripts/ai_query.py` gère les deux
sans configuration :

- **VPS / local** : variable d'environnement `GEMINI_API_KEY` (clé gratuite
  sur https://aistudio.google.com/apikey). Le script l'ajoute en `?key=`
  dans l'URL.
- **Session cloud** : un « identifiant » (credential) configuré côté
  réglages de l'environnement (section « Identifiants API », pas
  « Variables d'environnement » — cette dernière est en clair et déconseillée
  pour un secret). Concrètement : type Bearer, en-tête personnalisé renommé
  `x-goog-api-key` (préfixe vide, valeur = la clé), site autorisé
  `generativelanguage.googleapis.com`. Le proxy réseau de la session injecte
  alors l'en-tête sans jamais exposer la clé au code — `GEMINI_API_KEY`
  reste absente de l'environnement, c'est normal et attendu dans ce mode
  (vérifié en conditions réelles le 20/09/2026).

Si aucun des deux n'est configuré, l'appel échoue avec un message Gemini
explicite (401/403/erreur d'authentification) — le dire à l'utilisateur et
continuer la tâche sans délégation plutôt que d'échouer en silence. Si
l'appel échoue avec une erreur *réseau* (pas une erreur d'authentification) :
depuis la session cloud, le réseau sortant est restreint (cf. CLAUDE.md pt 4,
"Réseau Trusted = paquets+GitHub seul") — vérifier que
`generativelanguage.googleapis.com` est bien autorisé (mode Custom) avant de
conclure à un bug du script.

## Syntaxe d'appel

```bash
# Résumer un log VPS
python3 scripts/ai_query.py "Résume l'erreur et donne la cause racine" -f /path/to/logfile.log

# Depuis un pipe
docker logs radar-web --tail 200 | python3 scripts/ai_query.py "Trouve les erreurs 500" --stdin

# Demande simple de boilerplate
python3 scripts/ai_query.py "Écris 5 cas limites pour une fonction de slugification de titres vinyle"
```
