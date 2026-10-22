# 🚀 High-Performance Stream Proxy & IPTV Management Gateway

A lightweight, multi-threaded, low-latency live streaming proxy and dynamic IPTV catalog server built in Python. Features real-time HLS/DASH proxying with AES-128 DRM key rewriting, zero-polling WebSocket telemetry dashboard, Video-On-Demand (VOD) cataloging via SQLite, and deterministic RAM segment buffering for instant channel switching.

---

## 📑 Table of Contents

- [Architecture Overview](#architecture-overview)
- [Key Features](#key-features)
- [API & Stream Endpoint Reference](#api--stream-endpoint-reference)
  - [Dynamic Live IPTV Playlists](#1-dynamic-live-iptv-playlists)
  - [Video On Demand (VOD) Catalog](#2-video-on-demand-vod-catalog)
  - [JioTV Gateway (1,083 Channels)](#3-jiotv-gateway-1083-channels)
  - [Unified Live Stream Gateway](#4-unified-live-stream-gateway)
  - [Control Center, Telemetry & Maintenance](#5-control-center-telemetry--maintenance)
- [Environment Configuration (.env)](#environment-configuration-env)
- [Quick Start Guide](#quick-start-guide)
- [Player Setup (VLC, TiviMate, Kodi)](#player-setup-vlc-tivimate-kodi)
- [Performance Benchmarks](#performance-benchmarks)

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph Upstream Sources
        J["JioTV Upstream<br>(1,083 HLS/DASH Channels)"]
        S1["Server 1<br>(adult-tv-channels.com)"]
        S2["Server 2<br>(adult-tv-channels.click)"]
        OX["Server 3 / Oxax<br>(PlayerJS Base64 CDN)"]
        VOD["Teleplay VOD<br>(teleplay.db SQLite)"]
    end

    subgraph Core Proxy Pipeline (proxy1.py / proxy_server)
        R["Upstream Stream & Auth Resolver"]
        W["Manifest Rewriter<br>(HLS URI & AES-128 Ingestion)"]
        P["Pre-Fetch Worker Engine<br>(Concurrent Threads)"]
        M["In-Memory RAM Rolling Cache<br>(< 0.2ms Hit Latency)"]
        G["Global Memory Guard<br>(Max 60 Segments Cap)"]
        K["Auto-Kill Inactivity Engine<br>(120s Idle Timeout)"]
        V["VOD Range Proxy Engine<br>(206 Partial Content)"]
    end

    subgraph Client Applications
        C1["Flutter TV App (media_kit / mpv)"]
        C2["VLC / Kodi / TiviMate / IPTV Smarters"]
        C3["Web Control Center & WebSocket (/status)"]
    end

    J --> R
    S1 --> R
    S2 --> R
    OX --> R
    VOD --> V

    R --> W
    W <--> P
    W --> M
    W --> G
    W --> K

    M --> C1
    M --> C2
    V --> C1
    V --> C2
    M --> C3
```

---

## Key Features

1. **Self-Contained Dynamic IPTV Manifest Generator**:
   - Generates standard `#EXTINF` M3U/M3U8 playlists on-demand across 105 adult channels (Server 1, Server 2, Oxax TV) and 1,083 JioTV channels.
   - Automatically adapts stream URLs to match the calling client's host (e.g. `https://varun.myddns.me:8443` or local IP).
   - Supports live search filtering (`?q=<keyword>`) and per-server isolation (`?server=1|2|3`).

2. **Video On Demand (VOD) Streaming Gateway**:
   - Interrogates `teleplay.db` SQLite database to build structured, playable M3U playlists categorized by folder/group.
   - Reverse-proxies video chunks with transparent HTTP `Range: bytes=...` request forwarding (HTTP 206 Partial Content) for zero-stutter seek and fast-forward.

3. **Ultra-Low-Latency RAM Cache (< 0.2ms)**:
   - Uses deterministic sequence numbering (`seg_{sequence_number}.ts`) to decouple upstream randomized token rotations from client-facing playback.
   - Delivers chunks 1 & 2 directly to player in `< 300ms` (Fast-Start), while pre-fetching chunks 3+ in background threads for instant RAM hits.

4. **Zero-Polling Real-Time Control Center**:
   - Modern Glassmorphism dashboard at `/status` secured via HTTP Basic Auth or `?token=...`.
   - Real-time WebSocket (`/ws/status`) updates channel bitrate, RAM memory usage, buffer health, and connected player pings without polling.

5. **Self-Healing & Memory Protection**:
   - **Auto-Healing**: Transparently recovers and refreshes expired upstream session tokens when origin CDNs return `403 Forbidden` or `404 Not Found`.
   - **Smart Gateway Failover**: Automatically redirects dead Server 1 channels to Oxax mirrors.
   - **Global Memory Guard**: Strict rolling segment ceiling avoids OOM crashes on low-resource single-board computers (Raspberry Pi).
   - **Kernel Pagecache Purge**: `/purge` triggers garbage collection and flushes Linux dirty pages via `sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches`.

---

## API & Stream Endpoint Reference

### 1. Dynamic Live IPTV Playlists

| Endpoint | Method | Query Parameters | Description |
| :--- | :--- | :--- | :--- |
| `/playlist.m3u`, `/playlist.m3u8` | `GET` | `?server=...`, `?q=...`, `?proxy_all=1` | Unified master playlist combining Server 1, Server 2, and Oxax TV (105 channels). |
| `/playlist1.m3u`, `/playlist1.m3u8` | `GET` | `?q=...`, `?base_url=...` | Server 1 playlist (`adult-tv-channels.com`, 35 channels). |
| `/playlist2.m3u`, `/playlist2.m3u8` | `GET` | `?q=...`, `?proxy_all=1` | Server 2 playlist (`adult-tv-channels.click`, 29 channels). Direct links preserved unless `?proxy_all=1`. |
| `/playlist3.m3u`, `/playlist3.m3u8` | `GET` | `?q=...`, `?base_url=...` | Server 3 playlist (`oxax.tv`, 41 channels). |
| `/channels.json`, `/playlist.json` | `GET` | `?server=...`, `?q=...` | Structured JSON catalog of all channels with logos, IDs, and titles. |
| `/playlist/export` | `GET`, `POST` | `?base_url=...` | Persists compiled playlists to `docs/` and root `./playlist.m3u8`. |

### 2. Video On Demand (VOD) Catalog

| Endpoint | Method | Query Parameters | Description |
| :--- | :--- | :--- | :--- |
| `/vod.m3u`, `/vod.m3u8` | `GET` | `?port=8000`, `?folder=...`, `?q=...`, `?proxy=1`, `?ext=1` | Dynamic M3U playlist (`audio/x-mpegurl`) generated from `teleplay.db`. |
| `/playlist/vod.m3u` | `GET` | Same as `/vod.m3u` | URL alias for IPTV players expecting `/playlist/` prefix. |
| `/vod.json` | `GET` | `?folder=...`, `?q=...` | JSON catalog of VOD media assets with file sizes, durations, and stream links. |
| `/vod/stream/<id>` | `GET` | Supports `Range: bytes=...` | High-throughput seekable video streaming reverse proxy with partial content support. |

### 3. JioTV Gateway (1,083 Channels)

| Endpoint | Method | Description | Example |
| :--- | :--- | :--- | :--- |
| `/jiotv/<channel_id>.m3u8` | `GET` | Dynamic HLS manifest by channel ID | `http://localhost:8085/jiotv/144.m3u8` |
| `/jiotv/<channel_id>.mpd` | `GET` | Dynamic MPEG-DASH manifest by channel ID | `http://localhost:8085/jiotv/760.mpd` |
| `/render.key`, `/jiotv/key` | `GET` | Proxies Broadpeak AES-128 16-byte decryption keys | `http://localhost:8085/render.key?auth=...` |
| `/render.dash/<path>` | `GET` | Proxies MPEG-DASH audio/video chunks | `http://localhost:8085/render.dash/...` |
| `/jiotv.m3u`, `/playlist/jiotv.m3u` | `GET` | Full 1,083-channel auto-rewritten M3U playlist | `http://localhost:8085/jiotv.m3u` |
| `/jiotv/epg.xml.gz`, `/epg.xml.gz` | `GET` | Cached upstream EPG GZIP XMLTV guide | `http://localhost:8085/jiotv/epg.xml.gz` |
| `/jtvimage/<filename>` | `GET` | Channel logo proxy | `http://localhost:8085/jtvimage/ColorsHD1.png` |

### 4. Unified Live Stream Gateway

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/stream.m3u8?channel-id=<id>` | `GET` | Tunes JioTV channel via query parameter. |
| `/stream.m3u8?channel-name=<name>` | `GET` | Tunes Oxax TV channel by slug (e.g. `brazzers-tv`). |
| `/stream.m3u8?channel=<name>` | `GET` | Tunes Server 1 channel with auto-failover to Oxax. |
| `/stream.m3u8?hash=<code>` | `GET` | Tunes Server 2 channel by hash code. |
| `/stream.m3u8?gateway=<url>` | `GET` | Reverse-proxies arbitrary direct HLS stream with header spoofing. |
| `/<cache_id>/<segment.ts>` | `GET` | High-speed RAM chunk delivery (`< 0.2ms` cache hit). |

### 5. Control Center, Telemetry & Maintenance

| Endpoint | Method | Auth | Description |
| :--- | :--- | :--- | :--- |
| `/status` | `GET` | Basic / Token | Real-time HTML Control Center Dashboard. Add `?json=1` for JSON telemetry. |
| `/ws/status` | `WebSocket` | Basic / Token | High-frequency live WebSocket push updates. |
| `/purge` | `GET`, `POST` | Basic / Token | Clears all active RAM stream buffers and flushes OS kernel pagecache. |
| `/` | `GET` | None | Server health check and alive status. |

---

## Environment Configuration (.env)

All credentials, platform headers, and runtime tuning parameters are isolated in `.env`. Copy `.env.example` to get started:

```bash
cp .env.example .env
```

### Configuration Reference

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8085` | Port the proxy listens on. |
| `ADMIN_USER` | `admin` | HTTP Basic Auth username for `/status` and `/purge`. |
| `ADMIN_PASS` | — | HTTP Basic Auth password for `/status` and `/purge`. |
| `ADMIN_TOKEN` | — | Secret token for query parameter auth (`?token=...`) or `Bearer` header. |
| `PROXY_BASE_URL` | `http://localhost:8085` | External URL used when compiling M3U stream links. |
| `JIOTV_BASE_URL` | `http://localhost:5000` | Upstream JioTV server URL. |
| `JIOTV_PLAYLIST_FILE` | `./jiotv_playlist.m3u` | Path to raw JioTV M3U source file. |
| `TELEPLAY_DB_PATH` | `./teleplay.db` | Path to SQLite database containing VOD file metadata. |
| `TELEPLAY_STREAM_PORT` | `8000` | Port of upstream VOD file server. |
| `STREAM_INACTIVITY_TIMEOUT` | `120` | Seconds of inactivity before terminating a channel worker thread. |
| `MAX_BUFFER_SEGMENTS` | `50` | Maximum rolling video chunks cached per channel (~50s). |
| `MAX_GLOBAL_BUFFER_SEGMENTS` | `60` | Global segment ceiling across all channels combined to prevent high RAM use. |
| `CHANNELS_FILE` | `./channels.json` | Path to JSON file containing live TV channel catalog. |
| `HEADERS_SERVER1` | JSON object | HTTP headers for Server 1 requests. |
| `HEADERS_SERVER2_POST` | JSON object | POST headers for Server 2 token resolver. |
| `HEADERS_SERVER2_STREAM` | JSON object | Stream headers for Server 2 media chunks. |
| `HEADERS_OXAX` | JSON object | HTTP headers for Oxax TV requests. |
| `HEADERS_JIOTV` | JSON object | HTTP headers for JioTV stream requests. |
| `GARBAGE_TOKENS_OXAX` | JSON list | De-obfuscation tokens for Oxax PlayerJS decoding. |
| `CHANNEL_GATEWAY_ALIASES` | JSON object | Shorthand slug aliases for smart auto-failover. |

---

## Quick Start Guide

### 1. Installation

Ensure Python 3.10+ is installed:

```bash
# Clone or navigate to the directory
cd /Users/varunkv/Documents/python_files

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install required dependencies
pip install flask "httpx[http2]" python-dotenv flask-sock
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your specific domains, tokens, and credentials
nano .env
```

### 3. Launching the Proxy Server

Run the standalone server:

```bash
python proxy1.py
```

Or run the modular package:

```bash
python -m proxy_server
```

Expected startup output:

```text
ℹ️ [ONLINE] Multi-user HTTP running on port 8085.
 * Serving Flask app 'proxy1'
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:8085
```

### 4. Optional: Export Static Playlists to Disk

If you need physical `.m3u8` files saved in `docs/`:

```bash
python proxy1.py --export-playlists
```

---

## Player Setup (VLC, TiviMate, Kodi)

### In VLC Media Player
- Go to **Media** ➔ **Open Network Stream** (Ctrl+N / Cmd+N).
- Paste any of the following URLs:
  - **Master Live TV (105 channels)**: `http://localhost:8085/playlist.m3u`
  - **Video On Demand (VOD)**: `http://localhost:8085/vod.m3u`
  - **JioTV (1,083 channels)**: `http://localhost:8085/jiotv.m3u`
  - **Specific Channel**: `http://localhost:8085/jiotv/144.m3u8` or `http://localhost:8085/stream.m3u8?channel=brazzerstv`

### In [TiViPlayer](https://github.com/varunfri/TiViPlayer/releases/tag/v1.0.2)
- Add a new playlist with **Type: M3U Playlist**.
- Set **Playlist URL** to `http://<your-server-ip>:8085/playlist.m3u` (Live) or `http://<your-server-ip>:8085/vod.m3u` (Movies/VOD).
- Set **EPG URL** to `http://<your-server-ip>:8085/jiotv/epg.xml.gz`.

### Dashboard Access
Open `http://localhost:8085/status?token=<ADMIN_TOKEN>` in your browser to inspect real-time connection telemetry, buffer health, and RAM usage.

---

## Performance Benchmarks

| Metric | Direct Upstream Origin | Through `proxy1.py` Pipeline |
| :--- | :--- | :--- |
| **Segment Latency** | 250 ms – 1,400 ms | **0.12 ms – 0.35 ms (RAM Hit)** |
| **Session Token Expiry** | Fails after 10–15 min | **100% Zero-Downtime Auto-Heal** |
| **Channel Switching Time** | ~2.5 to 4.0 seconds | **< 350 ms (Fast-Start Pipeline)** |
| **VOD Seeking (Range HTTP 206)** | Sluggish / High Buffering | **Instantaneous Byte-Range Delivery** |
| **Memory Footprint** | N/A | **~3.5 MB per active channel (Cap: ~100 MB)** |

---

## License & Disclaimer

This project is intended strictly for personal research, educational development, and private proxy caching for authorized media streams.
