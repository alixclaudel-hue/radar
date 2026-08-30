from playwright.sync_api import sync_playwright
import pandas as pd
import yt_dlp
import time
import random
import os
import requests

# ============ PARAMETRES A AJUSTER ============
CHANNEL_URL = "URL_DE_LA_CHAINE"
MAX_VIDEOS = 10  # None pour traiter toute la chaine
OUTPUT_FILE = "musiques_channel.xlsx"
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")  # clé retirée du code
COOKIES_FILE = "www.youtube.com_cookies.txt"
# ================================================


def get_genre_lastfm(artist, track):
    if not artist or not track:
        return None
    try:
        # On ne garde que le premier artiste en cas de featuring/multi-artistes
        first_artist = artist.split(',')[0].strip()
        url = "http://ws.audioscrobbler.com/2.0/"
        params = {
            'method': 'track.gettoptags',
            'artist': first_artist,
            'track': track,
            'api_key': LASTFM_API_KEY,
            'format': 'json'
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        tags = data.get('toptags', {}).get('tag', [])
        if tags:
            return ", ".join(t['name'] for t in tags[:2])
        return None
    except Exception:
        return None


# --- Etape 1 : lister les videos de la chaine ---
print("Recuperation de la liste des videos...")

ydl_opts = {
    'extract_flat': True,
    'quiet': True,
    'cookiefile': COOKIES_FILE,
}

video_ids = []
seen_ids = set()

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(CHANNEL_URL, download=False)
    entries = info.get('entries', [])
    for entry in entries:
        if entry is None:
            continue
        sub_entries = entry.get('entries', [entry]) if 'entries' in entry else [entry]
        for sub_entry in sub_entries:
            if sub_entry is None:
                continue
            vid_id = sub_entry.get('id')
            if vid_id and vid_id not in seen_ids:
                seen_ids.add(vid_id)
                video_ids.append(vid_id)

print(f"{len(video_ids)} videos uniques trouvees dans la chaine")

if MAX_VIDEOS:
    video_ids = video_ids[:MAX_VIDEOS]
    print(f"Limite au test : {len(video_ids)} videos")

# --- Etape 2 : reprise si le fichier existe deja ---
already_processed = set()
existing_rows = []

if os.path.exists(OUTPUT_FILE):
    df_existing = pd.read_excel(OUTPUT_FILE)
    already_processed = set(df_existing['video_id'].astype(str).tolist())
    existing_rows = df_existing.to_dict('records')
    print(f"Reprise : {len(already_processed)} videos deja traitees, seront ignorees")

results = existing_rows.copy()
videos_to_process = [vid for vid in video_ids if vid not in already_processed]

print(f"\n{len(videos_to_process)} videos a traiter dans cette session\n")

# --- Etape 3 : scraping Playwright du panneau Musique ---
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    for i, vid in enumerate(videos_to_process, 1):
        video_url = f"https://www.youtube.com/watch?v={vid}"
        print(f"[{i}/{len(videos_to_process)}] {video_url}")

        try:
            page.goto(video_url, timeout=30000)
            page.wait_for_timeout(2500)

            for consent_text in ["text=Tout accepter", "text=Accept all"]:
                try:
                    page.click(consent_text, timeout=2000)
                except:
                    pass

            try:
                page.click("tp-yt-paper-button#expand", timeout=3000)
            except:
                pass

            page.wait_for_timeout(1500)

            cards = page.query_selector_all("yt-video-attribute-view-model")
            seen_links = set()
            found_any = False

            for card in cards:
                title_el = card.query_selector("h1.ytVideoAttributeViewModelTitle")
                artist_el = card.query_selector("h4.ytVideoAttributeViewModelSubtitle")
                album_el = card.query_selector("span.ytVideoAttributeViewModelSecondarySubtitle")
                link_el = card.query_selector("a.ytVideoAttributeViewModelContentContainer")

                title = title_el.inner_text() if title_el else None
                artist = artist_el.inner_text() if artist_el else None
                album = album_el.inner_text() if album_el else None
                href = link_el.get_attribute("href") if link_el else None
                full_link = f"https://www.youtube.com{href}" if href else None

                if full_link and full_link not in seen_links:
                    seen_links.add(full_link)
                    found_any = True
                    genre = get_genre_lastfm(artist, title)
                    results.append({
                        'video_id': vid,
                        'video_source': video_url,
                        'track_title': title,
                        'artist': artist,
                        'album': album,
                        'track_link': full_link,
                        'genre': genre
                    })

            if not found_any:
                results.append({
                    'video_id': vid,
                    'video_source': video_url,
                    'track_title': None,
                    'artist': None,
                    'album': None,
                    'track_link': None,
                    'genre': None
                })
                print("   -> Aucune musique identifiee")
            else:
                print(f"   -> {len(seen_links)} morceau(x) trouve(s)")

        except Exception as e:
            print(f"   -> Erreur : {e}")
            results.append({
                'video_id': vid,
                'video_source': video_url,
                'track_title': 'ERREUR',
                'artist': str(e)[:100],
                'album': None,
                'track_link': None,
                'genre': None
            })

        if i % 10 == 0:
            pd.DataFrame(results).to_excel(OUTPUT_FILE, index=False)
            print(f"   [Sauvegarde intermediaire - {len(results)} lignes]")

        time.sleep(random.uniform(4, 8))

    browser.close()

# --- Sauvegarde finale ---
pd.DataFrame(results).to_excel(OUTPUT_FILE, index=False)
print(f"\nTermine ! {OUTPUT_FILE} mis a jour avec {len(results)} lignes au total")
