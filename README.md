<p align="center"><img src="assets/icon.png" width="96" alt=""></p>

# IPTV Library

A self-hosted web app for your NAS that lets you browse, play and download the **movies and TV shows** on your own IPTV subscription.
It runs in Docker on UGREEN, Synology, Unraid or any other Docker host, and you use it from any browser on your network.

- **Works with most providers:** Xtream Codes login (server + username + password) or an M3U playlist URL
- **Movies & TV Shows** with posters, categories, search, sort, plots, cast and ratings
- **Play** in the browser, choosing the **audio language and subtitles** while you watch, or open any title in VLC
- **Live TV for Plex & Jellyfin:** tick the channels you want and they appear in Plex or Jellyfin with a full **TV guide (EPG)**
- **Download** movies, single episodes, whole seasons or entire shows straight to your NAS, laid out the way
  **Plex and Jellyfin** expect, with **posters, backgrounds, season posters, episode thumbnails and .nfo info files**
  - choose **separate folders for Movies and TV Shows** with a built-in folder browser
- **Audio & subtitles:** when a movie or episode has more than one language or audio type, Download asks which to keep (English is pre-selected).
  The download keeps just your choice (no re-encoding, so there's no quality loss), with an optional `.srt` file that Plex and Jellyfin pick up.
  - progress, speed and time left, and downloads resume after a dropped connection or a restart
- **Export** any list as an `.m3u` playlist or a `.csv` spreadsheet
- **One-click updates** from this repo's releases
- Optional password, and your IPTV login is stored only in your browser

> Use it only with an IPTV service you're subscribed to, and check your provider's terms about downloading.

## Install with Docker Compose

1. Make a folder for it on your NAS, e.g. `/volume1/docker/iptvarr`.
2. Save the compose file below as `docker-compose.yml` in that folder, then change two things:
   - `/volume1/Media` → the shared folder your downloads go in (it must already exist)
   - `IPTV_APP_PASSWORD` → pick a password, or anyone on your network can use the app
3. Start it:
   - **UGREEN:** Docker app → **Project** → **Create** → choose that folder → paste the compose file → **Deploy**
   - **Anywhere else:** `docker compose up -d`
4. Open `http://<your-NAS-IP>:8765`, then click **⬇ Downloads → Browse…** to pick where Movies and TV Shows are saved.

```yaml
# IPTV Library - Docker Compose (UGREEN NAS, Synology, Unraid, any Docker host)
# https://github.com/Fudgeamin99/iptvarr
#
# On first start the app downloads itself from the latest GitHub release into ./app.
# After that, use the "Update" button inside the app to get new versions.
#
# Open  http://<your-NAS-IP>:8765  from any phone, PC or TV browser on your network.

services:
  iptv-library:
    image: python:3.12-alpine          # official Python image - nothing to build
    container_name: iptv-library
    restart: unless-stopped
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        if ! command -v ffmpeg >/dev/null 2>&1; then
          echo "Installing FFmpeg for audio & subtitle choices (first start only)..."
          apk add --no-cache ffmpeg >/dev/null 2>&1 || echo "FFmpeg couldn't be installed - audio/subtitle choices will be off"
        fi
        if [ ! -f /app/iptv_library.py ]; then
          echo "Downloading IPTV Library from GitHub..."
          python -c "import urllib.request as u; u.urlretrieve('https://github.com/Fudgeamin99/iptvarr/releases/latest/download/iptv_library.py', '/app/iptv_library.py')"
        fi
        exec python /app/iptv_library.py --host 0.0.0.0 --port 8765 --no-browser
    ports:
      - "8765:8765"                    # change the LEFT number if 8765 is already used on your NAS
    environment:
      PYTHONUNBUFFERED: "1"
      IPTV_DOWNLOADS: /downloads
      IPTV_BROWSE_ROOT: /downloads     # the in-app folder picker stays inside your download share(s)
      IPTV_AUDIO_LANG: eng             # default audio language (change any time in the app)
      IPTV_SUB_LANG: eng               # default subtitle language, or "off"
      IPTV_CONFIG_DIR: /config
      IPTV_UMASK: "000"                # downloaded files can be moved/deleted by any NAS user over SMB
      # Recommended - otherwise anyone on your Wi-Fi can open the app.
      # Your browser asks for this username + password once.
      IPTV_APP_USER: iptv
      IPTV_APP_PASSWORD: ""            # e.g. "pick-a-password"; leave "" for no password
    volumes:
      - ./app:/app                     # the app itself (must be writable so the Update button works)
      - ./config:/config               # settings + download queue
      # >>> Where movies & shows are saved: change the LEFT side to a shared folder on your NAS <<<
      # Pick the exact Movies and TV Shows folders inside it from the app (Downloads > Browse...).
      - /volume1/Media:/downloads
      # Movies and TV in separate shares? Use these two lines instead of the one above:
      # - /volume1/Movies:/downloads/Movies
      # - /volume1/TV:/downloads/TV
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 30s
```

On first start the container installs FFmpeg (for audio and subtitle choices) and downloads the app from the latest release into `./app`, so the first start takes a minute. You don't need to build an image.
The same file is also in the repo as [`docker-compose.yml`](docker-compose.yml) and attached to every release.

### Settings

| Environment variable | What it does |
|---|---|
| `IPTV_DOWNLOADS` | Download share inside the container (keep it as `/downloads` and change the volume instead) |
| `IPTV_MOVIES_DIR` / `IPTV_TV_DIR` | Optional starting folders for Movies and TV Shows (default `/downloads/Movies` and `/downloads/TV Shows`; you can change them in the app) |
| `IPTV_BROWSE_ROOT` | Keeps the in-app folder picker inside this folder |
| `IPTV_AUDIO_LANG` / `IPTV_SUB_LANG` | Starting audio and subtitle languages (`eng`, `spa`, `fre`, …; subtitles can be `off`). You can change them in the app |
| `IPTV_CONFIG_DIR` | Where settings and the download queue are saved |
| `IPTV_APP_PASSWORD` / `IPTV_APP_USER` | Password-protect the app (user defaults to `iptv`) |
| `IPTV_UMASK` | Permission mask for downloaded files (`000` lets every NAS user move/delete them) |
| `IPTV_REPO` | GitHub repo the Update button checks (default `Fudgeamin99/iptvarr`) |

Keep **"At the same time"** in the Downloads panel at 1 unless your IPTV plan allows more connections, or your other devices may get cut off.

## Audio & subtitles

**Downloading:** if a movie or episode has more than one audio track or any subtitles, **⬇ Download** shows what's inside the file,
e.g. *English · AC3 5.1*, *Spanish · AAC Stereo*, *English (forced)*, and asks what to keep. For a season or a whole show, you choose
once from the first episode and the choice is applied to every episode by language. Titles with nothing to choose download straight away.
The languages that are pre-selected (English) are set under **⬇ Downloads → Audio & subtitles**, along with whether to keep the
other audio languages and whether to also save an `.srt`.

**Watching in the browser:** the player has audio and subtitle menus. FFmpeg on your NAS copies the picture, converts the chosen
audio to AAC stereo and streams it, so `.mkv` files and AC3/DTS audio that browsers can't normally play work too. HEVC (H.265)
video only plays in browsers that support it; use **Open in VLC** otherwise.

What you can pick depends on your provider: many IPTV files only have one audio track and no subtitles. Downloads are copied with
FFmpeg (not re-encoded), and if anything goes wrong the original file is kept. Picture-based subtitles (PGS/DVD) can be kept inside
`.mkv` downloads but can't be shown in the browser or turned into `.srt`.

## Downloads in Plex & Jellyfin

Point a Plex/Jellyfin **Movies** library at your Movies folder and a **TV Shows** library at your TV Shows folder. Each download is saved as:

```
Movies/Heat (1995) {tmdb-949}/
    Heat (1995).mkv          Heat (1995).nfo          Heat (1995).en.srt
    poster.jpg               background.jpg
TV Shows/Aggretsuko (2018) {tmdb-79581}/
    poster.jpg   background.jpg   tvshow.nfo   season01-poster.jpg
    Season 01/
        Season01.jpg
        Aggretsuko (2018) - S01E01 - Jumping into Society.mkv   (+ .nfo, .jpg, -thumb.jpg, .en.srt)
```

- **Names are cleaned up** so they match online: IPTV tags like `NF - `, `|EN|`, `[4K]`, `MULTI` and `HEVC` are removed and the year is kept.
- The **`{tmdb-…}` ID** in the folder name (when your provider has one) makes Plex match the exact film or show.
- **Artwork** comes from your provider (in higher resolution when it's a TMDB image) and is named for both apps:
  `poster`/`background` for both, `SeasonXX.jpg` for Plex and `seasonXX-poster.jpg` for Jellyfin, `<episode>.jpg` for Plex and `<episode>-thumb.jpg` for Jellyfin.
- **.nfo files** (title, year, plot, genres, cast, runtime, TMDB ID) are read by Jellyfin automatically, and by Plex's NFO agent if you use it.
- In Plex, keep **Use local assets** turned on in the library's advanced settings (it's on by default).
- Existing files are never overwritten. Turn the extras off under **⬇ Downloads → Save posters, artwork & info files**.

## Live TV in Plex & Jellyfin

Open the **Live TV** tab, tick the channels you want (or **Tick all shown** for a whole category), and press **Save channels**.
Then click **📺 Plex & Jellyfin setup** for the addresses to paste:

| | Plex (needs Plex Pass) | Jellyfin |
|---|---|---|
| Tuner | Live TV & DVR → Set up → enter `NAS-IP:8765` manually | Live TV → Tuner Devices → **M3U Tuner** → `http://NAS-IP:8765/live/channels.m3u` |
| Guide | "Have an XMLTV guide…?" → `http://NAS-IP:8765/live/epg.xml` | TV Guide Data Providers → **XMLTV** → `http://NAS-IP:8765/live/epg.xml` |

- The app acts as an HDHomeRun network tuner for Plex and serves an M3U playlist for Jellyfin. Both only contain your ticked channels.
- The **guide (EPG)** comes from your provider. It's filtered to your channels and renumbered to match, so they line up automatically,
  and it refreshes every 12 hours. You can also point it at a different XMLTV source.
- Channel numbers stay the same when you add or remove channels, so the guide matching in Plex/Jellyfin doesn't break.
- **Channels watched at once** limits how many streams run together. Keep it within your IPTV plan's connection limit.
- These addresses work without the app password (Plex and Jellyfin can't enter one). Your IPTV login is saved on the NAS
  (`config/livetv.json`) so they can tune channels.

## Updating

When a new release is published, an **⬆ Update** button appears in the app's top bar. Click it and the app
downloads the new version, checks it, replaces itself and restarts. Any downloads in progress pick up where they left off.
Click the version number next to the logo to check at any time.

## Publishing a new version

Change `VERSION = "x.y.z"` near the top of `iptv_library.py` and push to `main`.
The **Release** workflow checks the app, creates release `vx.y.z` with `iptv_library.py` and `docker-compose.yml` attached,
and every install's Update button picks it up. Pushes that don't change the version don't create a release.

## Without Docker

You can also run it directly with Python 3.8+ (no packages needed): `python3 iptv_library.py`, which opens `http://127.0.0.1:8765`.
