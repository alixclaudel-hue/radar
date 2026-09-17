---
name: diag (archivé)
description: Ancien emplacement du contrat de la session diagnostic VPS. Déplacé le 17/09 vers `.claude/skills/vps-ops/SKILL.md` (skill actif, auto-découvert par toute session Claude Code sur ce dépôt). Ne plus éditer ce fichier — il ne sera plus lu.
---

# Déplacé vers `.claude/skills/vps-ops/SKILL.md`

Ce fichier contenait, jusqu'au 17/09, le contrat de fonctionnement de la
session diagnostic VPS. Il vivait volontairement hors de `.claude/skills/`
(cf. `CLAUDE.md` pt 5 historique) tant que la boucle automatique `diag-vps`
et l'usage de cette session restaient en pause/incertains.

**Pourquoi le déplacement (17/09)** : ce contrat visait nommément la session
« Radar — VPS (diagnostic) », mais l'utilisateur travaille en pratique avec
une session VPS différente (« Session VPS », toujours active) qui n'a jamais
eu de raison de savoir que ce fichier existait — un fichier dans
`docs/archive/` n'est lu par personne automatiquement. Devenu un skill actif
nommé (`vps-ops`), il est désormais listé et disponible d'office pour
**toute** session Claude Code qui tourne sur ce dépôt, quel que soit son nom.

Le contenu à jour — interdits, périmètre élargi le 17/09 (jobs/Docker),
format de rapport — vit maintenant dans `.claude/skills/vps-ops/SKILL.md`.
Historique de l'arbitrage : `CLAUDE.md` pt 59.
