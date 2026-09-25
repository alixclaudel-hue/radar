# Accès mobile et interfaces — PC et iPhone sur la même session

Procédure d'accès au VPS pour les deux interfaces prévues au plan
[`delegation-multimodel-v1.md`](../plans/delegation-multimodel-v1.md:244) § 9 : VSCode
sur le poste de travail, client SSH mobile sur l'iPhone. **Les deux se branchent sur
la même session `tmux` existante** — il n'y a rien à installer côté serveur, et rien
de nouveau à maintenir.

Toutes les valeurs de ce document ont été relevées sur le VPS le 25/09/2026. Ce qui
n'a pas pu être vérifié sans `sudo` est listé en § 7, jamais supposé.

---

## 1. Le principe : une session, deux fenêtres sur la même

```
        PC pro (VSCode)                       iPhone (Blink / Termius)
   ssh ubuntu@radar-vps                  ssh ubuntu@100.94.157.91
   (Remote-SSH, ou terminal)             (Tailscale déjà connecté)
                 \                              /
                  \                            /
                   +---  tmux attach -t claude  ---+
                                |
                     une seule session Claude Code
                     un seul écrivain, un seul contexte
```

Conséquences à ne pas perdre de vue :

- **Une seule session Claude Code tourne à la fois.** Depuis l'iPhone on s'attache à
  celle qui est déjà lancée, on n'en ouvre pas une seconde. Deux sessions
  concurrentes écriraient dans le même dépôt sans se voir — c'est exactement le
  scénario que la règle « écrire à un seul des deux modèles » interdit.
- Le téléphone est un **terminal**, pas un second agent : mêmes outils, mêmes
  hooks, mêmes droits. Rien de spécifique à l'iPhone dans le code.
- Fermer le terminal (PC ou téléphone) ne tue pas la session : `tmux` continue,
  et c'est ce qui permet de passer d'un appareil à l'autre en cours de chantier.

---

## 2. Le VPS en chiffres (relevé du 25/09/2026)

| Élément | Valeur mesurée |
|---|---|
| Hôte | `vps-4294f993` |
| Utilisateur | `ubuntu` |
| Nom Tailscale | `radar-vps` |
| IP Tailscale (IPv4) | `100.94.157.91` |
| IP publique (IPv4) | `57.128.180.93` |
| Checkout de production | `/home/ubuntu/radar` |
| Atelier | `/home/ubuntu/radar-work` |
| Sessions `tmux` | `claude` (session de travail), `radar` (seconde session, ouverte le 17/09) |
| `sshd` | écoute sur `0.0.0.0:22` et `[::]:22` |
| Authentification SSH | **clé uniquement** — `PasswordAuthentication no` |
| Clés autorisées | `radar-deploy`, `github-actions-radar-deploy` (ED25519) |
| Tableau de bord `radar-ops` | `100.94.157.91:8610` (Tailscale) et `127.0.0.1:8610` (loopback) |
| Pare-feu | `ufw` **actif** (`systemctl is-active` → `active`) |

Deux points d'attention immédiats :

- Le port `8610` de `radar-ops` **n'est pas exposé sur l'IP publique** — seulement en
  loopback et sur l'interface Tailscale. C'est voulu, et c'est ce qui rend le
  tableau de bord joignable depuis l'iPhone sans ouvrir de port public.
- Le port `8600` (autre service) n'écoute **que** en loopback : il n'est atteignable
  ni depuis le PC ni depuis le téléphone, et ne passe pas par Caddy.

---

## 3. Interface PC — VSCode + tunnel SSH

La procédure habituelle, rappelée ici pour être au même endroit que le reste :

1. Ouvrir VSCode, `Remote-SSH: Connect to Host…`.
2. Se connecter à la cible du VPS — l'hôte `radar-vps` (Tailscale) ou
   `ubuntu@57.128.180.93`, selon la configuration du poste.
3. Une fois la fenêtre distante ouverte, dans le terminal intégré :

   ```sh
   tmux attach -t claude
   ```

   Si la session n'existe plus (VPS redémarré), la recréer :

   ```sh
   tmux new -s claude
   ```

4. Travailler depuis `/home/ubuntu/radar-work`, jamais depuis `/home/ubuntu/radar`
   (atelier contre production — contrat [`vps-ops`](../.claude/skills/vps-ops/SKILL.md:255)).

> Recommandation : passer par l'adresse Tailscale plutôt que l'IP publique. Le
> `sshd` écoute bien sur `0.0.0.0:22`, mais rien n'oblige à l'atteindre depuis
> l'Internet ouvert quand le tailnet suffit. Un alias `Host radar-vps` /
> `HostName 100.94.157.91` dans le `~/.ssh/config` du poste rend le choix explicite.

---

## 4. Interface iPhone — Tailscale + client SSH mobile

Aucun composant à construire : on réutilise l'app Tailscale et un client SSH.

### 4.1 Tailscale

1. Installer l'app **Tailscale** depuis l'App Store et se connecter avec **le même
   compte** que celui du VPS (`alix.claudel@` — l'hôte y est déjà enregistré sous
   `radar-vps`).
2. Vérifier que l'appareil apparaît connecté. Sur le VPS, `tailscale status` doit
   alors lister l'iPhone :

   ```
   100.94.157.91   radar-vps            alix.claudel@  linux
   100.83.59.57    iphone-12-mini       alix.claudel@  iOS
   ```

3. Utiliser l'IP Tailscale `100.94.157.91` dans le client SSH. **Ne pas** se servir
   de l'IP publique : c'est tout l'intérêt du tailnet.

### 4.2 Client SSH (Blink Shell, Termius, ou autre)

- Hôte : `100.94.157.91`
- Utilisateur : `ubuntu`
- Authentification : par clé (mot de passe désactivé côté serveur, voir § 2). La clé
  utilisée doit correspondre à l'une des entrées de `~/.ssh/authorized_keys` du VPS
  (`radar-deploy`), ou en ajouter une dédiée au téléphone si l'on préfère un secret
  distinct par appareil.
- Commande d'attache, identique au PC :

  ```sh
  ssh ubuntu@100.94.157.91
  tmux attach -t claude
  ```

### 4.3 Voir les sessions disponibles

Si l'on ne sait plus quelle session existe après une reconnexion :

```sh
tmux ls
# claude: 1 windows (created Fri Sep 11 17:06:05 2026)
# radar:  1 windows (created Thu Sep 17 21:12:50 2026)
tmux attach -t claude
```

`radar` est une seconde session ouverte le 17/09 ; la session de travail courante est
`claude`. En cas de doute, `tmux ls` puis s'attacher à celle qui porte l'invite de
Claude Code déjà lancée.

---

## 5. `tmux` — les gestes qu'on utilise vraiment

| Besoin | Commande / raccourci |
|---|---|
| S'attacher à la session | `tmux attach -t claude` |
| La détacher (revenir au shell, session maintenue) | `Ctrl-b` puis `d` |
| Lister les sessions | `tmux ls` |
| Créer une fenêtre dans la session | `Ctrl-b` puis `c` |
| Passer à la fenêtre suivante | `Ctrl-b` puis `n` |
| Renommer la fenêtre | `Ctrl-b` puis `,` |
| Tuer la session | `tmux kill-session -t claude` (à éviter : c'est la session de travail) |

Le détachement (`Ctrl-b d`) est le geste central : on quitte le terminal sans
interrompre le chantier, et on le retrouve à l'identique depuis l'autre appareil.

### 5.1 Redimensionnement du terminal (PC étroit ↔ téléphone)

`tmux` adapte la vue à la taille du plus petit client attaché. Un téléphone en
portrait peut donc reflow le texte du PC. La taille de fenêtre se règle :

```sh
tmux show -g window-size        # valeur courante
tmux set -g window-size largest # la vue ne rétrécit plus quand le téléphone s'attache
```

`largest` conserve la taille du client le plus grand (le PC) au lieu de suivre le plus
petit (le téléphone). L'alternative `manual` fige complètement la taille.

> **Non appliqué dans cette session.** Ce réglage se pose en direct sur le VPS ; il
> n'a pas été modifié ici pour ne pas toucher à la session de travail depuis une
> session hors VPS. À poser au premier branchement du téléphone, si le texte du PC
> se retrouve tronqué.

---

## 6. Tableau de bord `radar-ops` depuis le téléphone

Une fois Tailscale connecté, `radar-ops` est joignable directement depuis le
navigateur du téléphone :

```
http://100.94.157.91:8610/
```

C'est l'intérêt d'avoir fait écouter le service sur l'interface Tailscale et non sur
l'IP publique : la page est accessible depuis n'importe quel appareil du tailnet, et
seulement de ceux-là. La page `/delegation` y affiche le quota du jour, la part
gratuit / payant, les modèles en cooldown et les occasions manquées (Phase 4).

---

## 7. Ce qui reste à vérifier (nécessite `sudo` ou un vrai appareil)

Ces points n'ont **pas** pu être vérifiés depuis une session sans privilèges. Ils sont
consignés ici pour ne pas être confondus avec des faits établis, et repris dans le
TODO de `CLAUDE.md`.

- **Règles `ufw` effectives.** `ufw` est actif, mais `/etc/ufw/user.rules` et
  `/etc/ufw/user6.rules` sont lisibles uniquement par `root` (`Permission denied`).
  Les binaires `nft` et `iptables` sont absents du PATH — seul `/usr/sbin/ufw` est
  présent — donc l'ensemble de règles ne peut pas être déduit sans privilèges.
  À exécuter sur le VPS :

  ```sh
  sudo ufw status numbered
  ```

  Objectif fixé par le plan : « un contrôle des ACL Tailscale pour que le port SSH
  ne soit joignable que depuis les appareils de l'utilisateur »
  ([`delegation-multimodel-v1.md`](../plans/delegation-multimodel-v1.md:249)). Il faut
  confirmer que `22/tcp` est bien restreint à `tailscale0`, ou l'ouvrir uniquement
  depuis là si ce n'est pas le cas.

- **`/etc/ssh/sshd_config.d/50-cloud-init.conf`** est en `0600 root` : son contenu
  n'est pas lisible sans `sudo`. Le `60-cloudimg-settings.conf` lu, lui, pose
  `PasswordAuthentication no` — c'est la seule source vérifiée sur la politique
  d'authentification. `sshd_config` (fichier principal) est également illisible
  directement depuis cette session.

- **Branchement réel de l'iPhone.** La chaîne Tailscale + client SSH + `tmux attach`
  décrite en § 4 n'a pas été jouée depuis un téléphone dans cette session. À valider
  au premier usage, en particulier le comportement de redimensionnement (§ 5.1) et la
  lisibilité de `radar-ops` en portrait.

---

## 8. Récapitulatif des commandes

```sh
# PC (VSCode Remote-SSH, terminal intégré)
tmux attach -t claude

# iPhone (Tailscale connecté, client SSH)
ssh ubuntu@100.94.157.91
tmux ls
tmux attach -t claude

# Détacher sans interrompre : Ctrl-b puis d

# Tableau de bord, depuis n'importe quel appareil du tailnet
# http://100.94.157.91:8610/
```
