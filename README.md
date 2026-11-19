<p align="center"><img src="assets/icon.png" width="96" alt=""></p>

# IPTV Library

A self-hosted web app for your NAS that lets you browse, play and download the **movies and TV shows** on your own IPTV subscription.
It runs in Docker on UGREEN, Synology, Unraid or any other Docker host, and you use it from any browser on your network.

- **Works with most providers:** Xtream Codes login (server + username + password) or an M3U playlist URL
- **Movies & TV Shows** with posters, categories, search, sort, plots, cast and ratings
- **Play** in the browser, or open any title in VLC
- **Download** movies, single episodes, whole seasons or entire shows straight to your NAS, saved as
  `Movies/Title (Year).mkv` and `TV Shows/Show/Season 01/Show S01E01 - Title.mp4`
  - progress, speed and time left, and downloads resume after a dropped connection or a restart
- **Export** any list as an `.m3u` playlist or a `.csv` spreadsheet
- **One-click updates** from this repo's releases
- Optional password, and your IPTV login is stored only in your browser

> Use it only with an IPTV service you're subscribed to, and check your provider's terms about downloading.

## Install with Docker Compose

1. Make a folder for it on your NAS, e.g. `/volume1/docker/iptvarr`.
2. Download [`docker-compose.yml`](docker-compose.yml) into it and edit two things:
   - `/volume1/Media/IPTV` → the shared folder you want downloads saved to (it must already exist)
   - `IPTV_APP_PASSWORD` → pick a password, or anyone on your network can use the app
3. Start it:
   - **UGREEN:** Docker app → **Project** → **Create** → choose that folder → paste or upload the compose file → **Deploy**
   - **Anywhere else:** `docker compose up -d`
4. Open `http://<your-NAS-IP>:8765`.

On first start the container downloads the app from the latest release into `./app`. You don't need to build an image.

### Settings

| Environment variable | What it does |
|---|---|
| `IPTV_DOWNLOADS` | Download folder inside the container (keep it as `/downloads` and change the volume instead) |
| `IPTV_CONFIG_DIR` | Where settings and the download queue are saved |
| `IPTV_APP_PASSWORD` / `IPTV_APP_USER` | Password-protect the app (user defaults to `iptv`) |
| `IPTV_UMASK` | Permission mask for downloaded files (`000` lets every NAS user move/delete them) |
| `IPTV_REPO` | GitHub repo the Update button checks (default `Fudgeamin99/iptvarr`) |

Keep **"At the same time"** in the Downloads panel at 1 unless your IPTV plan allows more connections, or your other devices may get cut off.

## Updating

When a new release is published, an **⬆ Update** button appears in the app's top bar. Click it and the app
downloads the new version, checks it, replaces itself and restarts. Any downloads in progress pick up where they left off.
Click the version number next to the logo to check at any time.

## Publishing a new version

1. Change `VERSION = "x.y.z"` near the top of `iptv_library.py` and commit.
2. Tag and push: `git tag vx.y.z && git push origin vx.y.z` (or create a release on GitHub with that tag).

The **Release** workflow checks the version and attaches `iptv_library.py` and `docker-compose.yml` to the release.
Every install's Update button then picks it up.

## Without Docker

You can also run it directly with Python 3.8+ (no packages needed): `python3 iptv_library.py`, which opens `http://127.0.0.1:8765`.
