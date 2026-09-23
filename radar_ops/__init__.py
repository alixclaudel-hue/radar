"""Application de maintenance et de diagnostic de Radar.

Service Docker SÉPARÉ (`radar-ops`) et non une section de `radar_web` : un
outil de diagnostic qui meurt avec l'application qu'il surveille ne sert à rien
précisément au moment où on en a besoin. Il partage l'image, le volume `/data`
et le mécanisme de session (`radar_web.radar.websession`), mais pas le
processus.

Accès en LECTURE SEULE sur les DONNÉES : aucune route ne lance, n'arrête ni ne
modifie un job, une base ou un fichier de l'utilisateur. Ce tableau de bord
observe, il ne répare pas — toute action corrective reste du ressort de
l'application principale ou de la session VPS
(cf. `.claude/skills/vps-ops/SKILL.md`).

Deux écritures subsistent, héritées des modules partagés et sans rapport avec
les données : la création des dossiers de `/data` à l'import de `paths`, et
celle de `.session_secret` si l'appli principale ne l'a pas encore posé.
"""
