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
2. Save the compose file below as `docker-compose.yml` in that folder, then change two things:
   - `/volume1/Media/IPTV` → the shared folder you want downloads saved to (it must already exist)
   - `IPTV_APP_PASSWORD` → pick a password, or anyone on your network can use the app
3. Start it:
   - **UGREEN:** Docker app → **Project** → **Create** → choose that folder → paste the compose file → **Deploy**
   - **Anywhere else:** `docker compose up -d`
4. Open `http://<your-NAS-IP>:8765`.

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
      - /volume1/Media/IPTV:/downloads
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 30s
```

On first start the container downloads the app from the latest release into `./app`. You don't need to build an image.
The same file is also in the repo as [`docker-compose.yml`](docker-compose.yml) and attached to every release.

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

Change `VERSION = "x.y.z"` near the top of `iptv_library.py` and push to `main`.
The **Release** workflow checks the app, creates release `vx.y.z` with `iptv_library.py` and `docker-compose.yml` attached,
and every install's Update button picks it up. Pushes that don't change the version don't create a release.

## Without Docker

You can also run it directly with Python 3.8+ (no packages needed): `python3 iptv_library.py`, which opens `http://127.0.0.1:8765`.
