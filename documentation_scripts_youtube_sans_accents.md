# Documentation technique - Pipeline extraction musique YouTube

Ce document resume le fonctionnement de deux scripts Python developpes pour un usage personnel de curation musicale (DJ sets House/Tech House, chaine YouTube "themuddshow"). Objectif : extraire les musiques identifiees par YouTube dans les videos d'une chaine, puis alimenter une playlist YouTube personnelle pour tri manuel.

Environnement : Windows, execution locale (pas de cloud/Colab, pour eviter les blocages IP de YouTube).

---

## Script 1 : Extraction de la base de donnees musicale

**Fichier** : `script_complet_2.py` (ou nom similaire)
**Objectif** : parcourir toutes les videos d'une chaine YouTube et extraire les morceaux identifies dans le panneau "Musique" affiche sous chaque video, avec leur lien YouTube et leur genre musical.

### Pourquoi cette approche (contexte important)

Plusieurs methodes plus simples ont ete testees et rejetees :
- **API YouTube Data v3** : n'expose pas du tout les donnees du panneau "Musique".
- **yt-dlp seul** (champs `track`/`artist`/`album`) : ces champs existent dans yt-dlp mais restent vides pour ce type de contenu (DJ sets) - verifie en dumpant l'integralite du JSON retourne par yt-dlp pour une video dont on savait, par capture d'ecran, que le panneau "Musique" contenait 3 morceaux. Aucune trace des titres/artistes dans les donnees yt-dlp.
- **Conclusion** : le panneau "Musique" est charge dynamiquement en JavaScript par la page YouTube et n'est accessible qu'en simulant un vrai navigateur. D'ou le choix de **Playwright** (automatisation de navigateur headless) plutot qu'un scraping HTTP classique.

### Etapes du script

1. **Lister les videos de la chaine** via `yt-dlp` (`extract_flat: True`), deduplication par ID video (`video_id` = identifiant unique YouTube, 11 caracteres).
2. **Reprise automatique** : si le fichier Excel de sortie existe deja, les `video_id` deja presents sont exclus du traitement (permet de relancer le script apres interruption sans tout refaire).
3. **Pour chaque video restante**, via Playwright (navigateur Chromium headless) :
   - Charger la page `https://www.youtube.com/watch?v={id}`
   - Accepter le bandeau de consentement cookies si present
   - Cliquer sur le bouton "...plus" pour deplier la description
   - Chercher les elements `<yt-video-attribute-view-model>` (selecteur CSS correspondant aux cartes du panneau "Musique")
   - Extraire pour chaque carte : titre (`h1.ytVideoAttributeViewModelTitle`), artiste (`h4.ytVideoAttributeViewModelSubtitle`), album (`span.ytVideoAttributeViewModelSecondarySubtitle`), et lien YouTube du morceau (`a.ytVideoAttributeViewModelContentContainer`, attribut `href`)
   - **Deduplication interne** : YouTube affiche parfois ce panneau deux fois dans le DOM ; dedoublonnage par lien unique.
4. **Enrichissement genre musical** via l'API Last.fm (`track.gettoptags`), a partir du couple artiste/titre. Limite connue : les morceaux electroniques underground sont souvent peu/pas tagues sur Last.fm (retour vide frequent) - ce n'est pas un bug, c'est un manque de donnees cote Last.fm.
5. **Sauvegarde progressive** dans un fichier Excel (`.to_excel`) toutes les 10 videos, pour ne rien perdre en cas d'interruption.
6. **Anti-blocage YouTube** : delai aleatoire (4 a 8 secondes) entre chaque video, execution en local (pas sur IP mutualisee type Colab, qui se fait bloquer en 429 tres rapidement par YouTube).

### Colonnes du fichier Excel de sortie (`musiques_channel.xlsx`)

| Colonne | Contenu |
|---|---|
| `video_id` | ID YouTube de la video source (identifiant unique) |
| `video_source` | URL complete de la video source |
| `track_title` | Titre du morceau identifie |
| `artist` | Artiste(s) du morceau |
| `album` | Album/EP source |
| `track_link` | Lien YouTube du morceau (souvent une video "topic" officielle) |
| `genre` | Tags Last.fm (peut etre vide) |

### Limites connues
- Ne couvre que les videos ou YouTube a reussi une identification automatique (Content ID) - pas de garantie de couverture a 100%.
- Necessite un fichier de cookies YouTube valide (`www.youtube.com_cookies.txt`, export via extension navigateur) pour l'etape de listing des videos via yt-dlp - sans quoi blocage 429/bot-detection.
- Le genre Last.fm est un complement, pas une source fiable a 100% (creux important sur la musique electronique independante).

---

## Script 2 : Alimentation automatique d'une playlist YouTube

**Fichier** : `add_to_playlist.py`
**Objectif** : lire le fichier Excel genere par le Script 1 et ajouter automatiquement chaque morceau (via son `track_link`) dans une playlist YouTube personnelle nommee "Musiques scrap a trier", destinee a un tri manuel (garder/supprimer).

### Authentification

Contrairement au Script 1 (lecture de donnees publiques), l'ajout a une playlist personnelle necessite une **authentification OAuth2** complete (pas une simple cle API) :
- Identifiants OAuth "Application de bureau" crees sur Google Cloud Console, fichier `client_secret.json`.
- Scope utilise : `https://www.googleapis.com/auth/youtube` (acces complet ; pas de scope plus restreint disponible specifiquement pour les playlists).
- Le jeton d'acces est mis en cache localement (`token.pickle`) pour eviter une reconnexion a chaque lancement. **Limite connue** : en mode "Test" (app OAuth non publiee), Google invalide parfois le jeton apres ~7 jours (erreur `invalid_grant`), necessitant de supprimer `token.pickle` et de se reconnecter manuellement.

### Gestion du quota API (contrainte majeure)

- Quota par defaut : 10 000 unites/jour par projet Google Cloud, renouvele a minuit heure du Pacifique (~9h en France).
- `playlistItems.insert` coute 50 unites par appel → environ 190-200 ajouts maximum par jour.
- Le script est limite volontairement a `MAX_TO_ADD = 190` par session pour rester dans une marge de securite, et gere proprement l'erreur `quotaExceeded` (arret propre avec message, pas de plantage) pour reprendre le lendemain.
- Pas de cout financier : l'API est gratuite, le seul mecanisme de limitation est ce quota.

### Logique anti-doublon (double mecanisme)

Le point le plus important du script, ajoute apres un cas d'usage precis : l'utilisateur trie manuellement la playlist (supprime les morceaux non retenus) au fur et a mesure des imports quotidiens. Il ne faut donc **jamais reimporter un morceau deja propose une fois**, meme s'il a ete supprime de la playlist depuis.

Deux sources sont combinees pour determiner ce qui a deja ete "traite" :
1. **Contenu actuel de la playlist** (`playlistItems.list`) - ce qui y est encore present.
2. **Fichier d'historique local** (`historique_ajouts.txt`) - un ID YouTube par ligne, ecrit **immediatement** apres chaque ajout reussi (pas seulement en fin de script, pour ne rien perdre si le quota s'arrete en plein milieu). Ce fichier n'est jamais purge automatiquement et constitue la memoire permanente du tri.

```
already_handled = history_ids | current_playlist_ids
to_add = [vid for vid in video_ids if vid not in already_handled]
```

Un script utilitaire separe (`create_history_from_excel.py`) permet d'initialiser retroactivement l'historique a partir des N premiers morceaux du fichier Excel, dans les cas ou l'historique n'a pas ete alimente des le debut (ex. premiers tests manuels).

### Etapes du script

1. Lecture du fichier Excel, extraction des `video_id` depuis la colonne `track_link` (regex sur les 11 caracteres d'ID YouTube), deduplication.
2. Authentification OAuth (avec cache token).
3. Recherche de la playlist par nom exact (`playlists.list`, `mine=True`) ; creation automatique en mode prive si elle n'existe pas.
4. Recuperation de la liste actuelle des videos dans la playlist + de l'historique local.
5. Calcul de la liste a ajouter = tout ce qui n'est ni dans la playlist actuelle, ni dans l'historique - limite a `MAX_TO_ADD`.
6. Boucle d'ajout (`playlistItems.insert`), avec ecriture immediate dans l'historique a chaque succes, et gestion differenciee des erreurs :
   - `quotaExceeded` → arret propre de la boucle, message d'instruction pour le lendemain
   - `videoNotFound` / `playlistItemNotFound` → morceau ignore et marque comme traite (inutile de retenter une video supprimee/privee)
   - autres erreurs → loguees, script continue avec le morceau suivant
7. Bilan de session affiche en fin d'execution.

### Usage recurrent

Ce script est concu pour etre **relance quotidiennement** (une fois le quota renouvele) jusqu'a epuisement complet de la base de donnees (~3000+ morceaux → environ 16-17 jours au rythme de 190/jour). Aucune action manuelle requise entre les lancements autre que le tri dans l'interface YouTube.

---

## Fichiers impliques (resume)

| Fichier | Role |
|---|---|
| `script_complet_2.py` | Extraction musique + genre depuis les videos YouTube (Script 1) |
| `musiques_channel.xlsx` | Base de donnees de sortie du Script 1, entree du Script 2 |
| `www.youtube.com_cookies.txt` | Cookies session YouTube, requis par yt-dlp pour lister les videos |
| `add_to_playlist.py` | Alimentation de la playlist YouTube (Script 2) |
| `client_secret.json` | Identifiants OAuth Google Cloud (Script 2) |
| `token.pickle` | Cache du jeton d'authentification OAuth (Script 2) |
| `historique_ajouts.txt` | Memoire permanente anti-doublon des morceaux deja proposes (Script 2) |
| `create_history_from_excel.py` | Utilitaire d'initialisation retroactive de l'historique |
