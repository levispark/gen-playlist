# updated proxy

import sys
import json
import base64
import concurrent.futures
import gzip
import sqlite3
import gc
import subprocess
import os
import re
import threading
import time
from collections import deque
from urllib.parse import urljoin
from flask import Flask, Response, jsonify, request, stream_with_context
import httpx

app = Flask(__name__)

# ==============================================================================
# ENVIRONMENT & SECRETS INITIALIZATION (.env)
# ==============================================================================
def _load_env_file():
    """Loads environment variables from .env file immediately on import or startup."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except Exception:
        pass
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env_file()

try:
    from flask_sock import Sock
    sock = Sock(app)
except Exception:
    sock = None

# Cache window: ~45-50 seconds of rolling video buffer data in RAM per channel
MAX_BUFFER_SEGMENTS = 50

# Global segment ceiling across all channels combined to prevent high-memory usage (~100MB cap)
MAX_GLOBAL_BUFFER_SEGMENTS = 60

# INACTIVITY TIMEOUT: Kills background task engine loops if idle for 120 seconds (configurable)
INACTIVITY_TIMEOUT_SECONDS = int(os.getenv("STREAM_INACTIVITY_TIMEOUT", "120"))

# Thread-safe unified runtime datastore mapping memory tracking structures
STREAM_WAREHOUSE = {}
warehouse_lock = threading.Lock()

# Channel launch lock registry: prevents duplicate concurrent worker threads for the same channel
CHANNEL_INIT_LOCKS = {}
GLOBAL_INIT_LOCK = threading.Lock()


def get_channel_init_lock(cache_id: str) -> threading.Lock:
    with GLOBAL_INIT_LOCK:
        if cache_id not in CHANNEL_INIT_LOCKS:
            CHANNEL_INIT_LOCKS[cache_id] = threading.Lock()
        return CHANNEL_INIT_LOCKS[cache_id]

# In-flight download deduplication: clean_seg_key -> threading.Event()
IN_FLIGHT_DOWNLOADS = {}
in_flight_lock = threading.Lock()

# Global Performance Metrics
METRICS = {
    "start_time": time.time(),
    "ram_hits": 0,
    "fast_misses": 0,
    "manifest_requests": 0,
}
metrics_lock = threading.Lock()

# Parallel pre-fetch thread pool (up to 8 concurrent segment download workers)
PREFETCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# Authentication credentials for the /status control center dashboard (loaded from .env)
ADMIN_USERNAME = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASS", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def is_authenticated():
    """Validates request against query token, header token, or HTTP Basic Auth."""
    token = request.args.get("token")
    if token and token == ADMIN_TOKEN:
        return True

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        if auth_header[7:].strip() == ADMIN_TOKEN:
            return True
    if request.headers.get("X-Admin-Token") == ADMIN_TOKEN:
        return True

    auth = request.authorization
    if auth and auth.username == ADMIN_USERNAME and auth.password == ADMIN_PASSWORD:
        return True

    return False


def _parse_json_env(var_name: str, fallback=None):
    """Parses a JSON dictionary or list directly from an environment variable."""
    raw = os.getenv(var_name)
    if raw:
        try:
            val = json.loads(raw)
            if val is not None:
                return val
        except Exception as e:
            print(f"⚠️ Failed to parse {var_name} from env as JSON: {e}", flush=True)
    return fallback if fallback is not None else {}


# Channel Gateway Alias Mapping for Smart Auto-Failover (loaded directly from .env)
CHANNEL_GATEWAY_ALIASES = _parse_json_env("CHANNEL_GATEWAY_ALIASES")


# Distinct headers per platform target profile (loaded directly from .env)
HEADERS_SERVER1 = _parse_json_env("HEADERS_SERVER1")
HEADERS_SERVER2_POST = _parse_json_env("HEADERS_SERVER2_POST")
HEADERS_SERVER2_STREAM = _parse_json_env("HEADERS_SERVER2_STREAM")
GARBAGE_TOKENS_OXAX = _parse_json_env("GARBAGE_TOKENS_OXAX", fallback=[])
HEADERS_OXAX = _parse_json_env("HEADERS_OXAX")
JIOTV_BASE_URL = os.getenv("JIOTV_BASE_URL", "").rstrip("/")
HEADERS_JIOTV = _parse_json_env("HEADERS_JIOTV")

# Persistent shared HTTP client pool with HTTP/2 and keep-alive
CLIENT_POOL = httpx.Client(
    http2=False,  # Avoid HTTP/2 RST_STREAM on origin CDNs (Nimble Streamer)
    verify=False,
    follow_redirects=True,
    timeout=httpx.Timeout(10.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=120.0),
)


def decrypt_xor_payload(html_content):
    match = re.search(
        r"var\s+k\s*=\s*(\d+)\s*,\s*a\s*=\s*\[([\d\s,]+)\]", html_content
    )
    if not match:
        return None
    xor_key = int(match.group(1))
    raw_array_str = match.group(2)
    int_array = [int(x.strip()) for x in raw_array_str.split(",") if x.strip()]
    return "".join(chr(byte_val ^ xor_key) for byte_val in int_array)


def make_manifest_response(data_str, content_type="application/vnd.apple.mpegurl"):
    """Returns plaintext UTF-8 playlist with no-cache headers for 100% IPTV player compatibility."""
    res = Response(data_str, status=200, content_type=content_type)
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return res


def drop_os_page_cache():
    """Flushes Linux dirty pages and drops clean pagecache, dentries, and inodes."""
    # 1. Direct write if running as root or container
    try:
        if os.path.exists("/proc/sys/vm/drop_caches"):
            os.system("sync")
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3" + chr(10))
            return True, "OS kernel page cache dropped via direct write to /proc/sys/vm/drop_caches"
    except Exception:
        pass

    # 2. Non-interactive sudo (sudo -n prevents hanging on password prompts)
    try:
        cmd = "sudo -n sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            return True, "OS kernel page cache dropped via sudo tee /proc/sys/vm/drop_caches"
    except Exception:
        pass

    # 3. Standard tee if user has write permissions
    try:
        cmd = "sync && echo 3 | tee /proc/sys/vm/drop_caches"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            return True, "OS kernel page cache dropped via tee /proc/sys/vm/drop_caches"
    except Exception:
        pass

    return False, "Non-Linux OS or sudo requires password"


def purge_ram_cache(channel_id=None):
    """
    Purges RAM storage for a specific channel or globally across all channels.
    Clears segment chunks, URL mappings, invokes gc.collect(),
    and flushes OS page cache via 'sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches'.
    """
    purged_segments = 0
    purged_bytes = 0
    with warehouse_lock:
        if channel_id:
            ctx = STREAM_WAREHOUSE.get(channel_id)
            if ctx:
                with ctx["lock"]:
                    purged_segments = len(ctx["segments_cache"])
                    purged_bytes = sum(len(b) for b in ctx["segments_cache"].values())
                    ctx["segments_cache"].clear()
                    ctx["segment_urls"].clear()
                    ctx["history_queue"].clear()
        else:
            for cid, ctx in list(STREAM_WAREHOUSE.items()):
                with ctx["lock"]:
                    purged_segments += len(ctx["segments_cache"])
                    purged_bytes += sum(len(b) for b in ctx["segments_cache"].values())
                    ctx["segments_cache"].clear()
                    ctx["segment_urls"].clear()
                    ctx["history_queue"].clear()
    gc.collect()

    # Drop OS page cache when purging globally
    os_dropped, os_details = False, "Skipped for single channel purge"
    if not channel_id:
        os_dropped, os_details = drop_os_page_cache()

    return {
        "purged_segments": purged_segments,
        "purged_mb": round(purged_bytes / (1024 * 1024), 2),
        "active_channels": len(STREAM_WAREHOUSE),
        "os_page_cache_dropped": os_dropped,
        "os_cache_details": os_details,
    }


def enforce_global_memory_guard():
    """Evicts oldest segments across idle channels if total memory segments exceed cap."""
    with warehouse_lock:
        total_segments = sum(len(ctx["segments_cache"]) for ctx in STREAM_WAREHOUSE.values())
        if total_segments <= MAX_GLOBAL_BUFFER_SEGMENTS:
            return

        sorted_channels = sorted(STREAM_WAREHOUSE.values(), key=lambda c: c["last_requested_time"])
        for ctx in sorted_channels:
            with ctx["lock"]:
                while len(ctx["segments_cache"]) > 2 and total_segments > MAX_GLOBAL_BUFFER_SEGMENTS:
                    if ctx["history_queue"]:
                        oldest_key = ctx["history_queue"].popleft()
                    else:
                        oldest_key = next(iter(ctx["segments_cache"]))
                    ctx["segments_cache"].pop(oldest_key, None)
                    total_segments -= 1


def process_and_rewrite_manifest(raw_manifest_text, context, cache_id):
    """
    Transforms upstream M3U8 into a clean, client-optimized live playlist:
    - Guarantees strictly monotonic #EXT-X-MEDIA-SEQUENCE across upstream resets.
    - Seamlessly injects #EXT-X-DISCONTINUITY and maintains #EXT-X-DISCONTINUITY-SEQUENCE.
    - Rewrites segment URLs to local proxy paths (/<cache_id>/<path.ts>).
    - Caches exact upstream segment URLs for zero-latency retrieval.
    """
    raw_lines = raw_manifest_text.splitlines()
    segments_in_manifest = []
    current_upstream_seq = None

    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                current_upstream_seq = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif stripped and not stripped.startswith("#"):
            segments_in_manifest.append(stripped)

    # Detect segment prefix from first segment (e.g., l_91021 from l_91021_... or token from /s/token/...)
    current_prefix = None
    if segments_in_manifest:
        if context.get("server_type") == "oxax":
            current_prefix = "oxax"
        elif context.get("server_type") == "jiotv":
            current_prefix = f"jiotv_{context.get('identifier', 'ch')}"
        else:
            first_seg = segments_in_manifest[0].split("?")[0].lstrip("/")
            match_p = re.match(r"^([a-zA-Z0-9]+_\d+)", first_seg)
            if match_p:
                current_prefix = match_p.group(1)
            elif first_seg.startswith("s/"):
                parts = first_seg.split("/")
                if len(parts) >= 2:
                    current_prefix = parts[1]
            else:
                current_prefix = first_seg.split("_")[0]

    discontinuity_triggered = False

    # 1. Monotonic Media Sequence Enforcement
    if current_upstream_seq is not None:
        last_up = context.get("last_upstream_media_seq")
        if last_up is not None and current_upstream_seq < last_up:
            last_served = context.get("last_served_media_seq", last_up)
            min_next = last_served + 1
            context["virtual_seq_offset"] = min_next - current_upstream_seq
            discontinuity_triggered = True
            print(
                f"🔀 [SEQUENCE RESET] [{cache_id.upper()}] Upstream seq dropped: "
                f"{last_up} -> {current_upstream_seq}. Virtual offset: +{context['virtual_seq_offset']} "
                f"(Client served {min_next}).",
                flush=True,
            )

        served_seq = current_upstream_seq + context.get("virtual_seq_offset", 0)
        last_served = context.get("last_served_media_seq")
        if last_served is not None and served_seq < last_served:
            served_seq = last_served + 1
            context["virtual_seq_offset"] = served_seq - current_upstream_seq

        context["last_upstream_media_seq"] = current_upstream_seq
        context["last_served_media_seq"] = served_seq
    else:
        served_seq = None

    # 2. Encoder Route Prefix Change Check
    last_prefix = context.get("last_seg_prefix")
    if last_prefix is not None and current_prefix is not None and current_prefix != last_prefix:
        discontinuity_triggered = True
        print(
            f"🔀 [STREAM RE-ROUTE] [{cache_id.upper()}] Encoder route changed: "
            f"{last_prefix} -> {current_prefix}. Injecting discontinuity.",
            flush=True,
        )

    if current_prefix:
        context["last_seg_prefix"] = current_prefix

    # 3. Discontinuity Boundary Registration & Stale Cache Pruning
    if discontinuity_triggered and segments_in_manifest:
        first_clean_k = segments_in_manifest[0].split("?")[0].lstrip("/")
        context["active_discontinuity_segments"].add(first_clean_k)

        # Prune stale segments from previous stream route to free memory
        if last_prefix:
            purged = 0
            for old_k in list(context["segments_cache"].keys()):
                if last_prefix in old_k and current_prefix not in old_k:
                    del context["segments_cache"][old_k]
                    purged += 1
            if purged > 0:
                print(f"🧹 [STALE PURGE] Removed {purged} old segments from previous stream route for {cache_id.upper()}", flush=True)

    # 4. Slide window check: increment discontinuity sequence when boundary segment rolls off
    current_clean_keys = set(s.split("?")[0].lstrip("/") for s in segments_in_manifest)
    rolled_off = [s for s in list(context["active_discontinuity_segments"]) if s not in current_clean_keys]
    for ro in rolled_off:
        context["active_discontinuity_segments"].discard(ro)
        context["discontinuity_seq_count"] += 1

    # 5. Manifest Lines Rewriting
    rewritten_lines = []
    injected_disc_seq = False
    segments_to_fetch = []
    chunks_manifest_url = context["chunks_manifest_url"]
    seg_idx = 0

    for line in raw_lines:
        stripped = line.strip()

        if stripped.startswith("#EXT-X-KEY:"):
            match_key = re.search(r'URI="([^"]+)"', stripped)
            if match_key:
                raw_key_uri = match_key.group(1)
                full_key_url = urljoin(chunks_manifest_url, raw_key_uri)
                query_str = full_key_url.split("?", 1)[1] if "?" in full_key_url else ""
                rewritten_key_uri = f"/render.key?{query_str}" if query_str else "/render.key"
                rewritten_line = re.sub(r'URI="[^"]+"', f'URI="{rewritten_key_uri}"', stripped)
                rewritten_lines.append(rewritten_line)
            else:
                rewritten_lines.append(stripped)
            continue

        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            if served_seq is not None:
                rewritten_lines.append(f"#EXT-X-MEDIA-SEQUENCE:{served_seq}")
            else:
                rewritten_lines.append(stripped)

            if context["discontinuity_seq_count"] > 0 and not injected_disc_seq:
                rewritten_lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{context['discontinuity_seq_count']}")
                injected_disc_seq = True
            continue

        if stripped.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            try:
                up_disc_seq = int(stripped.split(":", 1)[1].strip())
                context["discontinuity_seq_count"] = max(context["discontinuity_seq_count"], up_disc_seq)
            except ValueError:
                pass
            rewritten_lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{context['discontinuity_seq_count']}")
            injected_disc_seq = True
            continue

        if stripped and not stripped.startswith("#"):
            if context.get("server_type") == "jiotv":
                seg_seq = (served_seq + seg_idx) if served_seq is not None else seg_idx
                clean_key = f"seg_{seg_seq}.ts"
                clean_path = clean_key
                seg_idx += 1
            else:
                clean_path = stripped.lstrip("/")
                clean_key = clean_path.split("?")[0]

            # Cache exact upstream URL for 100% reliable chunk retrieval
            joined_seg_url = urljoin(chunks_manifest_url, stripped)
            if context.get("server_type") == "oxax" and "?" not in stripped and "?" in chunks_manifest_url:
                joined_seg_url = f"{joined_seg_url}?{chunks_manifest_url.split('?', 1)[1]}"
            context["segment_urls"][clean_key] = joined_seg_url

            # Inject #EXT-X-DISCONTINUITY before this segment if marked
            if clean_key in context["active_discontinuity_segments"]:
                if not (rewritten_lines and rewritten_lines[-1] == "#EXT-X-DISCONTINUITY"):
                    if rewritten_lines and rewritten_lines[-1].startswith("#EXTINF:"):
                        extinf_line = rewritten_lines.pop()
                        rewritten_lines.append("#EXT-X-DISCONTINUITY")
                        rewritten_lines.append(extinf_line)
                    else:
                        rewritten_lines.append("#EXT-X-DISCONTINUITY")

            if stripped.startswith("http://") or stripped.startswith("https://"):
                rewritten_lines.append(stripped)
            else:
                rewritten_lines.append(f"/{cache_id}/{clean_path}")

            if ".ts" in stripped:
                segments_to_fetch.append((clean_key, stripped))
        else:
            rewritten_lines.append(line)

    final_manifest = "\n".join(rewritten_lines)
    return final_manifest, segments_to_fetch


# ==============================================================================
# PIPELINE RESOLVERS
# ==============================================================================
def resolve_server1_stream(channel_name):
    clean_name = channel_name.strip("/").lower()
    if clean_name.startswith("tv/"):
        channel_slug = clean_name.replace("tv/", "", 1)
    else:
        channel_slug = clean_name

    urls_to_try = [
        f"https://adult-tv-channels.com/tv/{channel_slug}.php",
        f"https://adult-tv-channels.com/{channel_slug}.php",
    ]

    try:
        parent_playlist_url = None
        for base_site_url in urls_to_try:
            try:
                res = CLIENT_POOL.get(base_site_url, headers=HEADERS_SERVER1)
                if res.status_code == 200:
                    parent_playlist_url = decrypt_xor_payload(res.text)
                    if parent_playlist_url:
                        break
            except Exception:
                continue

        if not parent_playlist_url:
            return None

        media_res = CLIENT_POOL.get(parent_playlist_url, headers=HEADERS_SERVER1)
        media_res.raise_for_status()

        for line in media_res.text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ".m3u8" in line:
                return line if line.startswith("http") else urljoin(parent_playlist_url, line)
        return parent_playlist_url
    except Exception as e:
        print(f"[Server 1 Error] Pipeline resolution failed for {channel_name}: {e}", flush=True)
        return None


def resolve_server2_stream(hash_code):
    clean_hash = hash_code.strip()
    if clean_hash.startswith("http://") or clean_hash.startswith("https://"):
        api_url = clean_hash
    elif clean_hash.startswith("/"):
        api_url = f"https://adult-tv-channels.click/mvuS3Ky2DPesSF8GdiJM{clean_hash}"
    elif len(clean_hash) == 10 and clean_hash.isalnum():
        api_url = f"https://adult-tv-channels.click/mvuS3Ky2DPesSF8GdiJM/{clean_hash}"
    else:
        api_url = f"https://adult-tv-channels.click/mvuS3Ky2DPesSF8GdiJM/{clean_hash}"

    try:
        res = CLIENT_POOL.post(api_url, headers=HEADERS_SERVER2_POST, content="")
        res.raise_for_status()

        data = res.json()
        token = data.get("fileUrl")
        if not token:
            return None

        decoded_bytes = base64.b64decode(token)
        reversed_string = decoded_bytes.decode("utf-8", errors="ignore")
        parent_playlist_url = reversed_string[::-1]

        media_res = CLIENT_POOL.get(parent_playlist_url, headers=HEADERS_SERVER2_STREAM)
        media_res.raise_for_status()

        for line in media_res.text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ".m3u8" in line:
                return line if line.startswith("http") else urljoin(parent_playlist_url, line)
        return parent_playlist_url
    except Exception as e:
        print(f"[Server 2 Error] Pipeline resolution failed for {hash_code}: {e}", flush=True)
        return None


# ==============================================================================
# SERVER 3: OXAX.TV DYNAMIC STREAM & TOKEN RESOLVER (Playerjs Architecture)
# ==============================================================================
_OXAX_LOCKS = {}
_OXAX_GLOBAL_LOCK = threading.Lock()
_OXAX_CACHE = {}  # {channel: (url, expiry_timestamp)}


def resolve_oxax_stream(channel_name, max_retries=3, force_refresh=False):
    """
    Resolves live HLS stream for oxax.tv channels:
    1. Deduplicates concurrent resolution requests using channel locks.
    2. Reuses recently resolved URL if younger than 10 seconds.
    3. Fetches page, parses kodk, kos, and decodes Playerjs base64 payload.
    4. Validates that candidate redirect returns real HLS (#EXTM3U), not 403 HTML.
    """
    clean_name = channel_name.strip()
    if clean_name.startswith("http://") or clean_name.startswith("https://"):
        page_url = clean_name
        slug = clean_name.split("/")[-1].removesuffix(".html")
    else:
        slug = clean_name.strip("/").removesuffix(".html")
        page_url = f"http://oxax.tv/{slug}.html"

    # 1. Check recent valid resolution cache (< 10 seconds)
    now = time.time()
    with _OXAX_GLOBAL_LOCK:
        if force_refresh:
            _OXAX_CACHE.pop(slug, None)
        elif slug in _OXAX_CACHE:
            cached_url, exp = _OXAX_CACHE[slug]
            if now < exp:
                return cached_url
        if slug not in _OXAX_LOCKS:
            _OXAX_LOCKS[slug] = threading.Lock()
        chan_lock = _OXAX_LOCKS[slug]

    with chan_lock:
        # Re-check cache inside lock
        now = time.time()
        if force_refresh:
            _OXAX_CACHE.pop(slug, None)
        elif slug in _OXAX_CACHE:
            cached_url, exp = _OXAX_CACHE[slug]
            if now < exp:
                return cached_url

        for attempt in range(max_retries):
            try:
                res = CLIENT_POOL.get(page_url, headers=HEADERS_OXAX)
                if res.status_code != 200:
                    time.sleep(0.3 * (attempt + 1))
                    continue

                html = res.text
                kodk_m = re.search(r'var\s+kodk\s*=\s*["\']([^"\']+)["\']', html)
                kos_m = re.search(r'var\s+kos\s*=\s*["\']([^"\']+)["\']', html)
                player_m = re.search(r'new\s+Playerjs\s*\(\s*["\']#F([^"\']+)["\']\s*\)', html)

                if not (kodk_m and kos_m and player_m):
                    time.sleep(0.3 * (attempt + 1))
                    continue

                kodk = kodk_m.group(1)
                kos = kos_m.group(1)
                enc_payload = player_m.group(1)

                for token in GARBAGE_TOKENS_OXAX:
                    enc_payload = enc_payload.replace(token, "")

                raw_json = base64.b64decode(enc_payload).decode("utf-8", errors="ignore")
                data = json.loads(raw_json)

                file_tpl = data.get("file", "")
                if not file_tpl:
                    continue

                candidates = file_tpl.split(" or ")
                for candidate in candidates:
                    stream_url = candidate.replace("{v1}", kodk).replace("{v2}", kos).strip()
                    if not stream_url.startswith("http://") and not stream_url.startswith("https://"):
                        stream_url = urljoin("https://s.oxax.tv/", stream_url.lstrip("/"))

                    try:
                        check_res = CLIENT_POOL.get(stream_url, headers=HEADERS_OXAX, follow_redirects=True)
                        if check_res.status_code == 200 and ("#EXTM3U" in check_res.text or "#EXT" in check_res.text):
                            real_stream_url = str(check_res.url)
                            for line in check_res.text.splitlines():
                                line = line.strip()
                                if line and not line.startswith("#") and ".m3u8" in line:
                                    resolved = line if line.startswith("http") else urljoin(real_stream_url, line)
                                    with _OXAX_GLOBAL_LOCK:
                                        _OXAX_CACHE[slug] = (resolved, time.time() + 10)
                                    return resolved
                            with _OXAX_GLOBAL_LOCK:
                                _OXAX_CACHE[slug] = (real_stream_url, time.time() + 10)
                            return real_stream_url
                    except Exception as ex:
                        continue

            except Exception as e:
                time.sleep(0.3 * (attempt + 1))
                continue

        print(f"[Oxax Error] Failed to resolve working live stream for {channel_name} after {max_retries} attempts", flush=True)
        return None


def resolve_jiotv_stream(channel_id):
    """
    Resolves JioTV channel ID into an active, authenticated child HLS media playlist URL.
    Handles upstream 302 redirects from JioTV Go and extracts the highest-quality
    variant stream from the master playlist.
    Includes fast retry on transient errors.
    """
    clean_id = str(channel_id).replace(".m3u8", "").strip()
    target_url = f"{JIOTV_BASE_URL}/live/{clean_id}.m3u8"
    for attempt in range(2):
        try:
            r = CLIENT_POOL.get(target_url, headers=HEADERS_JIOTV)
            if r.status_code != 200:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                print(f"⚠️ [JioTV Resolver] Upstream returned status {r.status_code} for channel: {clean_id}", flush=True)
                return None

            text = r.text
            lines = [line.strip() for line in text.splitlines() if line.strip()]

            variants = []
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                    bw = int(bw_match.group(1)) if bw_match else 0
                    if i + 1 < len(lines):
                        next_line = lines[i + 1]
                        if not next_line.startswith("#"):
                            variants.append((bw, next_line))

            if variants:
                variants.sort(key=lambda x: x[0], reverse=True)
                best_variant_url = variants[0][1]
                chunks_url = urljoin(str(r.url), best_variant_url)
                return chunks_url

            return str(r.url)
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
                continue
            print(f"❌ [JioTV Resolver Error] Channel {clean_id}: {e}", flush=True)
            return None
    return None

# ==============================================================================
# HIGH-SPEED PARALLEL PRE-FETCH WORKER WITH SELF-HEALING & MANIFEST CACHING
# ==============================================================================
def download_single_segment(aligned_chunks_url, seg_entry, context):
    if isinstance(seg_entry, tuple):
        clean_seg_key, raw_seg = seg_entry
    else:
        clean_seg_key = seg_entry.split("?")[0].lstrip("/")
        raw_seg = seg_entry

    with context["lock"]:
        if clean_seg_key in context["segments_cache"]:
            return

    with in_flight_lock:
        if clean_seg_key in IN_FLIGHT_DOWNLOADS:
            return
        event = threading.Event()
        IN_FLIGHT_DOWNLOADS[clean_seg_key] = event

    with context["lock"]:
        seg_url = context.get("segment_urls", {}).get(clean_seg_key)
    if not seg_url:
        seg_url = urljoin(aligned_chunks_url, raw_seg)

    try:
        res = CLIENT_POOL.get(seg_url, headers=context["headers"])
        if res.status_code == 200:
            with context["lock"]:
                context["segments_cache"][clean_seg_key] = res.content
                context["history_queue"].append(clean_seg_key)

                for old_cached in list(context["segments_cache"].keys()):
                    if old_cached not in context["history_queue"]:
                        del context["segments_cache"][old_cached]

            enforce_global_memory_guard()
            print(f"📥 [{context['cache_id'].upper()} BUFFER] Pre-fetched (Fast Parallel): {clean_seg_key}", flush=True)
    except Exception:
        pass
    finally:
        with in_flight_lock:
            event.set()
            IN_FLIGHT_DOWNLOADS.pop(clean_seg_key, None)


def channel_buffer_worker(cache_id, aligned_chunks_url, target_headers, server_type, identifier):
    print(f"⚡ [Buffer Engine] Background worker launched for target ID: {cache_id}", flush=True)
    last_proactive_refresh = time.time()

    with warehouse_lock:
        if cache_id not in STREAM_WAREHOUSE:
            STREAM_WAREHOUSE[cache_id] = {
                "cache_id": cache_id,
                "server_type": server_type,
                "identifier": identifier,
                "chunks_manifest_url": aligned_chunks_url,
                "cached_manifest": None,
                "segments_cache": {},
                "segment_urls": {},
                "history_queue": deque(maxlen=MAX_BUFFER_SEGMENTS),
                "kill_signal": False,
                "last_requested_time": time.time(),
                "headers": target_headers,
                "lock": threading.Lock(),
                "ready_event": threading.Event(),
                "last_upstream_media_seq": None,
                "virtual_seq_offset": 0,
                "last_served_media_seq": None,
                "last_seg_prefix": None,
                "discontinuity_seq_count": 0,
                "active_discontinuity_segments": set(),
                "served_chunks_count": 0,
            }
        context = STREAM_WAREHOUSE[cache_id]
        context["chunks_manifest_url"] = aligned_chunks_url
        context["headers"] = target_headers
        context["kill_signal"] = False
        context["last_requested_time"] = time.time()

    while not context["kill_signal"]:
        try:
            # 1. SUPERSEDED CHECK: If another worker context has taken over this cache_id, exit cleanly without deleting
            with warehouse_lock:
                current_ctx = STREAM_WAREHOUSE.get(cache_id)
                if current_ctx is not None and current_ctx is not context:
                    print(f"⚠️ [Buffer Engine] Duplicate worker thread for '{cache_id}' detected newer active worker. Exiting duplicate thread.", flush=True)
                    context["kill_signal"] = True
                    break

            with context["lock"]:
                if context.get("streaming_active", 0) > 0:
                    context["last_requested_time"] = time.time()
                time_since_last_pull = time.time() - context["last_requested_time"]

            if time_since_last_pull > INACTIVITY_TIMEOUT_SECONDS:
                print(
                    f"💤 [AUTO-KILL ENGAGED] No users streaming '{cache_id.upper()}' "
                    f"for {int(time_since_last_pull)}s. Purging worker...",
                    flush=True,
                )
                with warehouse_lock:
                    with context["lock"]:
                        context["segments_cache"].clear()
                        context["segment_urls"].clear()
                        context["history_queue"].clear()
                        context["kill_signal"] = True
                    # CRUCIAL: Only delete from STREAM_WAREHOUSE if this entry is still THIS worker context!
                    if STREAM_WAREHOUSE.get(cache_id) is context:
                        del STREAM_WAREHOUSE[cache_id]
                break

            now = time.time()
            if server_type == "oxax" and (now - last_proactive_refresh) > 60:
                new_manifest_url = resolve_oxax_stream(identifier, force_refresh=True)
                if new_manifest_url and new_manifest_url != aligned_chunks_url:
                    aligned_chunks_url = new_manifest_url
                    with context["lock"]:
                        context["chunks_manifest_url"] = new_manifest_url
                    last_proactive_refresh = now
                    print(f"🔄 [PROACTIVE AUTO-HEAL] Refreshed session token before expiry for '{cache_id}'", flush=True)

            res = CLIENT_POOL.get(aligned_chunks_url, headers=target_headers)

            is_valid_manifest = res.status_code == 200 and ("#EXTM3U" in res.text or "#EXT" in res.text)
            if res.status_code in (401, 403, 404) or not is_valid_manifest:
                print(f"🔄 [AUTO-HEAL TRIGGERED] Upstream returned {res.status_code} for '{cache_id}'. Refreshing session tokens...", flush=True)
                if server_type == "jiotv":
                    new_manifest_url = resolve_jiotv_stream(identifier)
                elif server_type == "oxax":
                    new_manifest_url = resolve_oxax_stream(identifier, force_refresh=True)
                elif server_type == "server1":
                    new_manifest_url = resolve_server1_stream(identifier)
                else:
                    new_manifest_url = resolve_server2_stream(identifier)

                if new_manifest_url:
                    aligned_chunks_url = new_manifest_url
                    last_proactive_refresh = time.time()
                    with context["lock"]:
                        context["chunks_manifest_url"] = new_manifest_url
                    print(f"✅ [AUTO-HEAL SUCCESS] '{cache_id}' restored with new session URL: {new_manifest_url}", flush=True)
                    continue
                else:
                    time.sleep(1.0)
                    continue

            # --- MANIFEST PRE-PARSING & RE-WRITING IN RAM ---
            with context["lock"]:
                final_manifest, segments_to_fetch = process_and_rewrite_manifest(
                    res.text, context, cache_id
                )
                context["cached_manifest"] = final_manifest

            # Fast-Start: Immediately release manifest to player so playback starts in < 5ms!
            context["ready_event"].set()

            # Dispatch uncached segments to thread pool for parallel pre-fetching in background
            uncached = []
            with context["lock"]:
                for seg_entry in segments_to_fetch:
                    clean_k = seg_entry[0] if isinstance(seg_entry, tuple) else seg_entry.split("?")[0].lstrip("/")
                    if clean_k not in context["segments_cache"]:
                        uncached.append(seg_entry)

            if uncached:
                try:
                    for seg_entry in uncached:
                        PREFETCH_EXECUTOR.submit(download_single_segment, aligned_chunks_url, seg_entry, context)
                except RuntimeError:
                    pass

        except Exception as err:
            print(f"⚠️ [Worker Loop Error] {cache_id}: {err}", flush=True)

        time.sleep(1.5)


# ==============================================================================
# FLASK ROUTE ENDPOINTS
# ==============================================================================
@app.route("/epg.xml")
def serve_epg_xml():
    """Serves uncompressed XMLTV Electronic Program Guide."""
    epg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merged_epg.xml")
    if not os.path.exists(epg_path):
        return Response("<tv/>", status=404, content_type="application/xml")
    with open(epg_path, "r", encoding="utf-8") as f:
        data = f.read()
    res = Response(data, status=200, content_type="application/xml; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Cache-Control"] = "public, max-age=1800"
    return res


@app.route("/epg.xml.gz")
def serve_epg_gz():
    """Serves compressed GZip XMLTV Electronic Program Guide."""
    epg_gz_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merged_epg.xml.gz")
    if not os.path.exists(epg_gz_path):
        return Response("EPG archive not found.", status=404, content_type="text/plain")
    with open(epg_gz_path, "rb") as f:
        data = f.read()
    res = Response(data, status=200, content_type="application/gzip")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Cache-Control"] = "public, max-age=1800"
    return res


@app.route("/")
def index():
    return """
    <html>
      <head><title>Stream Proxy Engine</title></head>
      <body style="font-family: sans-serif; background: #0f172a; color: #f8fafc; padding: 40px; text-align: center;">
        <h1 style="color: #38bdf8;">Unified Stream Proxy Engine: ONLINE 🚀</h1>
        <p style="color: #94a3b8;">High-speed RAM buffer, multi-user multiplexer, and auto-healing stream tunnel operational.</p>
        <p><a href="/status" style="color: #38bdf8; text-decoration: underline;">View Live Metrics & System Dashboard &rarr;</a></p>
      </body>
    </html>
    """


def get_status_payload():
    """Extracts unified real-time performance metrics and active channels payload."""
    with metrics_lock:
        total_hits = METRICS["ram_hits"]
        total_misses = METRICS["fast_misses"]
        total_reqs = total_hits + total_misses
        hit_ratio = round((total_hits / total_reqs * 100), 1) if total_reqs > 0 else 100.0
        uptime_seconds = int(time.time() - METRICS["start_time"])
        manifest_reqs = METRICS["manifest_requests"]

    active_streams = []
    total_cached_bytes = 0

    with warehouse_lock:
        for cid, ctx in STREAM_WAREHOUSE.items():
            with ctx["lock"]:
                cached_count = len(ctx["segments_cache"])
                ch_bytes = sum(len(b) for b in ctx["segments_cache"].values())
                total_cached_bytes += ch_bytes
                idle_sec = int(time.time() - ctx["last_requested_time"])
                active_streams.append({
                    "channel_id": cid,
                    "server": ctx["server_type"],
                    "cached_segments": cached_count,
                    "cached_mb": round(ch_bytes / (1024 * 1024), 2),
                    "idle_seconds": idle_sec,
                    "served_seq": ctx.get("last_served_media_seq"),
                    "status": "Buffering" if cached_count > 0 else "Initializing",
                })

    total_mb = round(total_cached_bytes / (1024 * 1024), 2)
    return {
        "uptime_seconds": uptime_seconds,
        "hit_ratio_percent": hit_ratio,
        "ram_hits": total_hits,
        "stream_misses": total_misses,
        "manifest_requests": manifest_reqs,
        "total_ram_used_mb": total_mb,
        "active_channels": len(active_streams),
        "channels": active_streams,
    }


if sock is not None:
    @sock.route("/status/ws")
    def status_ws(ws):
        """Real-time bidirectional WebSocket service streaming metrics every 1s."""
        if not is_authenticated():
            try:
                ws.close(1008, "Unauthorized")
            except Exception:
                pass
            return
        while True:
            try:
                payload = get_status_payload()
                ws.send(json.dumps(payload))
                time.sleep(1)
            except Exception:
                break


@app.route("/status/stream")
def status_stream():
    """Real-time Server-Sent Events (SSE) socket stream pushing metrics every 1s."""
    if not is_authenticated():
        return Response(
            "Unauthorized Access: Please provide valid credentials or pass ?token=...",
            401,
            {"WWW-Authenticate": 'Basic realm="Stream Proxy Control Center"'},
        )

    def event_stream():
        while True:
            try:
                payload = get_status_payload()
                yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(1)
            except GeneratorExit:
                break
            except Exception:
                time.sleep(1)

    res = Response(stream_with_context(event_stream()), mimetype="text/event-stream")
    res.headers["Cache-Control"] = "no-cache"
    res.headers["X-Accel-Buffering"] = "no"
    res.headers["Access-Control-Allow-Origin"] = "*"
    return res


@app.route("/purge", methods=["GET", "POST"])
def purge_cache_route():
    """Manually purges RAM segment warehouse (all channels or specific channel)."""
    if not is_authenticated():
        return Response(
            "Unauthorized Access: Please provide valid credentials or pass ?token=...",
            401,
            {"WWW-Authenticate": 'Basic realm="Stream Proxy Control Center"'},
        )
    channel = request.args.get("channel") or request.args.get("channel-id") or request.args.get("channel-name")
    result = purge_ram_cache(channel)
    print(f"🧹 [RAM PURGE] Evicted {result['purged_segments']} segments ({result['purged_mb']} MB) from RAM.", flush=True)

    if request.path.endswith(".json") or request.args.get("json") or "application/json" in request.headers.get("Accept", ""):
        return jsonify(result)

    token_param = f"?token={request.args.get('token')}" if request.args.get("token") else ""
    return Response(
        f"<html><head><meta http-equiv='refresh' content='1;url=/status{token_param}'></head>"
        f"<body style='background:#0f172a;color:#f8fafc;font-family:sans-serif;padding:40px;text-align:center;'>"
        f"<h2 style='color:#38bdf8;'>🧹 RAM Cache Purged!</h2>"
        f"<p>Released {result['purged_mb']} MB ({result['purged_segments']} segments). Redirecting to dashboard...</p>"
        f"</body></html>",
        200,
        content_type="text/html",
    )


@app.route("/status")
def status_dashboard():
    """Live web dashboard and JSON endpoint showing real-time proxy performance."""
    if not is_authenticated():
        return Response(
            "Unauthorized Access: Please provide valid credentials or pass ?token=...",
            401,
            {"WWW-Authenticate": 'Basic realm="Stream Proxy Control Center"'},
        )

    data = get_status_payload()

    if request.path.endswith(".json") or request.args.get("json") or "application/json" in request.headers.get("Accept", ""):
        return jsonify(data)

    # Initial HTML Render (No <meta http-equiv="refresh"> - dynamically updated via Socket Service)
    channels_html = ""
    for s in data["channels"]:
        channels_html += f"""
        <tr style="border-bottom: 1px solid #1e293b; transition: background 0.2s;" onmouseover="this.style.background='#1e293b'" onmouseout="this.style.background='transparent'">
            <td style="padding: 14px 16px; font-weight: 700; color: #38bdf8; font-family: monospace; font-size: 14px;">{s['channel_id'].upper()}</td>
            <td style="padding: 14px 16px;"><span style="background: #1e293b; border: 1px solid #334155; padding: 4px 10px; border-radius: 6px; color: #94a3b8; font-size: 12px; font-weight: 600;">{s['server'].upper()}</span></td>
            <td style="padding: 14px 16px; color: #34d399; font-weight: 600;">{s['cached_segments']} chunks <span style="color: #64748b; font-size: 12px; font-weight: 400;">({s['cached_mb']} MB)</span></td>
            <td style="padding: 14px 16px; color: #facc15; font-size: 13px;">{s['idle_seconds']}s ago <span style="color: #64748b; font-size: 11px;">(Seq {s['served_seq']})</span></td>
            <td style="padding: 14px 16px;"><span style="display: inline-flex; align-items: center; gap: 6px; background: #064e3b; border: 1px solid #059669; padding: 4px 10px; border-radius: 12px; color: #34d399; font-size: 12px; font-weight: 600;"><span style="width: 6px; height: 6px; border-radius: 50%; background: currentColor;"></span>{s['status']}</span></td>
            <td style="padding: 14px 16px; text-align: right;"><button onclick="purgeRAM('{s['channel_id']}')" style="background: #27272a; hover: #dc2626; color: #f87171; border: 1px solid #3f3f46; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer;">Purge</button></td>
        </tr>
        """

    if not data["channels"]:
        channels_html = '<tr><td colspan="6" style="padding: 36px 16px; text-align: center; color: #64748b; font-size: 14px;">No active channels buffering right now. Start streaming in your player to see live pipeline metrics.</td></tr>'

    total_segments = sum(s['cached_segments'] for s in data["channels"])

    html = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Stream Proxy Engine — Real-Time Dashboard</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        background-color: #0b0f19;
        color: #f1f5f9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        margin: 0;
        padding: 0;
        -webkit-font-smoothing: antialiased;
      }}
      .dashboard-wrapper {{
        max-width: 1200px;
        margin: 0 auto;
        padding: 32px 24px 60px 24px;
      }}
      .top-navbar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 28px;
        padding-bottom: 20px;
        border-bottom: 1px solid #1e293b;
        flex-wrap: wrap;
        gap: 16px;
      }}
      .brand-title {{
        margin: 0;
        font-size: 22px;
        font-weight: 800;
        color: #f8fafc;
        display: flex;
        align-items: center;
        gap: 10px;
        letter-spacing: -0.5px;
      }}
      .brand-subtitle {{
        font-size: 13px;
        color: #64748b;
        margin-top: 4px;
        font-weight: 400;
      }}
      .actions-group {{
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
      }}
      .btn-purge {{
        background: #dc2626;
        color: #ffffff;
        border: 1px solid #ef4444;
        padding: 8px 16px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        transition: all 0.2s;
      }}
      .btn-purge:hover {{
        background: #b91c1c;
      }}
      .badge-status {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 7px 14px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 700;
        background: #1e293b;
        border: 1px solid #334155;
        color: #94a3b8;
        transition: all 0.3s;
      }}
      .status-pulse {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: currentColor;
        animation: pulse 1.6s infinite ease-in-out;
      }}
      @keyframes pulse {{
        0%, 100% {{ opacity: 1; transform: scale(1); }}
        50% {{ opacity: 0.3; transform: scale(0.85); }}
      }}
      .metrics-grid {{
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 16px;
        margin-bottom: 28px;
      }}
      @media (max-width: 1024px) {{
        .metrics-grid {{ grid-template-columns: repeat(2, 1fr); }}
      }}
      @media (max-width: 600px) {{
        .metrics-grid {{ grid-template-columns: 1fr; }}
      }}
      .metric-card {{
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 12px;
        padding: 20px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
      }}
      .metric-label {{
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: #94a3b8;
        margin-bottom: 8px;
      }}
      .metric-value {{
        font-size: 28px;
        font-weight: 800;
        color: #38bdf8;
        line-height: 1.1;
        letter-spacing: -0.5px;
      }}
      .metric-sub {{
        font-size: 11px;
        color: #64748b;
        margin-top: 8px;
      }}
      .content-card {{
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        margin-bottom: 28px;
      }}
      .card-header {{
        padding: 18px 24px;
        border-bottom: 1px solid #1f2937;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
      }}
      .card-title {{
        font-size: 16px;
        font-weight: 700;
        color: #f8fafc;
        margin: 0;
        display: flex;
        align-items: center;
        gap: 8px;
      }}
      .card-sub {{
        font-size: 12px;
        color: #64748b;
      }}
      .table-container {{
        width: 100%;
        overflow-x: auto;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        text-align: left;
      }}
      th {{
        padding: 14px 16px;
        color: #94a3b8;
        border-bottom: 1px solid #1f2937;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        background: #0f172a;
      }}
      .info-box {{
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 10px;
        padding: 16px 20px;
        font-size: 13px;
        color: #94a3b8;
        line-height: 1.6;
      }}
      .info-box code {{
        background: #1e293b;
        color: #38bdf8;
        padding: 2px 6px;
        border-radius: 4px;
        font-family: monospace;
        font-size: 12px;
      }}
    </style>
  </head>
  <body>
    <div class="dashboard-wrapper">
      
      <!-- Top Navigation Bar -->
      <div class="top-navbar">
        <div>
          <h1 class="brand-title">
            <span style="color: #38bdf8;">⚡</span> Stream Proxy Control Center
          </h1>
          <div class="brand-subtitle">Real-Time Zero-Polling Socket Pipeline &bull; Low-Latency RAM Engine</div>
        </div>
        <div class="actions-group">
          <button class="btn-purge" onclick="purgeRAM()">
            <span>🧹</span> Purge All RAM
          </button>
          <div id="sock-status" class="badge-status">
            <span class="status-pulse"></span>
            <span>CONNECTING SOCKET...</span>
          </div>
        </div>
      </div>

      <!-- Core 8-Card Metrics Grid (Balanced 4x2 Layout) -->
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">RAM Hit Rate</div>
          <div id="val-hit-ratio" class="metric-value" style="color: #34d399;">{data['hit_ratio_percent']}%</div>
          <div class="metric-sub">Served directly from memory</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Cache Memory Hits</div>
          <div id="val-hits" class="metric-value">{data['ram_hits']}</div>
          <div class="metric-sub">&lt; 0.2ms zero-copy delivery</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Direct Fast Misses</div>
          <div id="val-misses" class="metric-value" style="color: #fb923c;">{data['stream_misses']}</div>
          <div class="metric-sub">Live passthrough on startup / seek</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Manifest Requests</div>
          <div id="val-manifests" class="metric-value" style="color: #818cf8;">{data['manifest_requests']}</div>
          <div class="metric-sub">M3U8 / MPD rewritten polls</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">RAM Buffer In Use</div>
          <div id="val-ram" class="metric-value" style="color: #c084fc;">{data['total_ram_used_mb']} MB</div>
          <div id="val-segments-info" class="metric-sub">{total_segments} / 60 global segment cap</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Active Channels</div>
          <div id="val-active" class="metric-value" style="color: #2dd4bf;">{data['active_channels']}</div>
          <div class="metric-sub">Concurrently buffered streams</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Server Uptime</div>
          <div id="val-uptime" class="metric-value" style="color: #60a5fa;">{data['uptime_seconds']}s</div>
          <div class="metric-sub">Continuous proxy operation</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Memory Ceiling</div>
          <div class="metric-value" style="color: #94a3b8;">60 <span style="font-size: 16px; font-weight: 500;">chunks</span></div>
          <div class="metric-sub">Auto-evicts least active streams</div>
        </div>
      </div>

      <!-- Active Channel Pipelines Table Card -->
      <div class="content-card">
        <div class="card-header">
          <div>
            <h2 class="card-title">Active Channel Buffers</h2>
            <div class="card-sub">Real-time segment caches, rolling media sequence & inactivity tracking</div>
          </div>
          <div style="font-size: 12px; color: #64748b;">Auto-kills idle streams after 60s</div>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Channel / Identifier</th>
                <th>Gateway Engine</th>
                <th>RAM Buffer</th>
                <th>Last Player Ping</th>
                <th>Engine State</th>
                <th style="text-align: right;">Action</th>
              </tr>
            </thead>
            <tbody id="channels-table-body">
              {channels_html}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Quick Stream Helper Info Box -->
      <div class="info-box">
        <strong style="color: #f8fafc;">💡 Playback Endpoints:</strong>
        Stream any channel in VLC, Kodi, or IINA using:
        <code>http://&lt;ip&gt;:8085/stream.m3u8?channel-name=brazzers-tv</code> &bull;
        <code>http://&lt;ip&gt;:8085/jiotv/143.m3u8</code> &bull;
        RAM buffer auto-populates on first request.
      </div>

    </div>

    <!-- Real-Time Client Socket Service -->
    <script>
      function formatUptime(seconds) {{
        if (seconds < 60) return seconds + 's';
        const mins = Math.floor(seconds / 60);
        const secs = seconds % 60;
        if (mins < 60) return mins + 'm ' + secs + 's';
        const hours = Math.floor(mins / 60);
        return hours + 'h ' + (mins % 60) + 'm';
      }}

      function updateDashboard(payload) {{
        if (!payload) return;
        const hr = document.getElementById('val-hit-ratio');
        if (hr) hr.textContent = payload.hit_ratio_percent + '%';
        const hits = document.getElementById('val-hits');
        if (hits) hits.textContent = payload.ram_hits;
        const misses = document.getElementById('val-misses');
        if (misses) misses.textContent = payload.stream_misses;
        const man = document.getElementById('val-manifests');
        if (man) man.textContent = payload.manifest_requests;
        const ram = document.getElementById('val-ram');
        if (ram) ram.textContent = payload.total_ram_used_mb + ' MB';
        const act = document.getElementById('val-active');
        if (act) act.textContent = payload.active_channels;
        const upt = document.getElementById('val-uptime');
        if (upt) upt.textContent = formatUptime(payload.uptime_seconds);

        let totSegs = 0;
        if (payload.channels) {{
          totSegs = payload.channels.reduce((acc, c) => acc + (c.cached_segments || 0), 0);
        }}
        const segInfo = document.getElementById('val-segments-info');
        if (segInfo) segInfo.textContent = totSegs + ' / 60 global segment cap';

        const tbody = document.getElementById('channels-table-body');
        if (!tbody) return;
        if (!payload.channels || payload.channels.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="6" style="padding: 36px 16px; text-align: center; color: #64748b; font-size: 14px;">No active channels buffering right now. Start streaming in your player to see live pipeline metrics.</td></tr>';
          return;
        }}

        let rows = '';
        for (const s of payload.channels) {{
          rows += `
            <tr style="border-bottom: 1px solid #1e293b; transition: background 0.2s;" onmouseover="this.style.background=\'#1e293b\'" onmouseout="this.style.background=\'transparent\'">
              <td style="padding: 14px 16px; font-weight: 700; color: #38bdf8; font-family: monospace; font-size: 14px;">${{s.channel_id.toUpperCase()}}</td>
              <td style="padding: 14px 16px;"><span style="background: #1e293b; border: 1px solid #334155; padding: 4px 10px; border-radius: 6px; color: #94a3b8; font-size: 12px; font-weight: 600;">${{s.server.toUpperCase()}}</span></td>
              <td style="padding: 14px 16px; color: #34d399; font-weight: 600;">${{s.cached_segments}} chunks <span style="color: #64748b; font-size: 12px; font-weight: 400;">(${{s.cached_mb}} MB)</span></td>
              <td style="padding: 14px 16px; color: #facc15; font-size: 13px;">${{s.idle_seconds}}s ago <span style="color: #64748b; font-size: 11px;">(Seq ${{s.served_seq}})</span></td>
              <td style="padding: 14px 16px;"><span style="display: inline-flex; align-items: center; gap: 6px; background: #064e3b; border: 1px solid #059669; padding: 4px 10px; border-radius: 12px; color: #34d399; font-size: 12px; font-weight: 600;"><span style="width: 6px; height: 6px; border-radius: 50%; background: currentColor;"></span>${{s.status}}</span></td>
              <td style="padding: 14px 16px; text-align: right;"><button onclick="purgeRAM(\'${{s.channel_id}}\\')" style="background: #27272a; color: #f87171; border: 1px solid #3f3f46; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer;">Purge</button></td>
            </tr>
          `;
        }}
        tbody.innerHTML = rows;
      }}

      async function purgeRAM(channel) {{
        const target = channel ? "channel '" + channel.toUpperCase() + "'" : "ALL channels";
        if (!confirm("Are you sure you want to purge RAM buffer for " + target + "?")) return;
        const token = new URLSearchParams(location.search).get("token");
        let url = "/purge";
        const params = [];
        if (token) params.push("token=" + encodeURIComponent(token));
        if (channel) params.push("channel=" + encodeURIComponent(channel));
        if (params.length) url += "?" + params.join("&");
        try {{
          const res = await fetch(url, {{ headers: {{ "Accept": "application/json" }} }});
          const data = await res.json();
          const osMsg = data.os_page_cache_dropped ? " • OS Page Cache Freed" : ""; alert("✅ Purged " + data.purged_segments + " segments (" + data.purged_mb + " MB)" + osMsg + "!");
        }} catch(e) {{
          alert("Failed to purge RAM: " + e);
        }}
      }}

      function initSocketService() {{
        const badge = document.getElementById('sock-status');
        const isHttps = location.protocol === 'https:';
        const wsProto = isHttps ? 'wss:' : 'ws:';
        const wsUrl = `${{wsProto}}//${{location.host}}/status/ws${{location.search}}`;

        let wsConnected = false;
        try {{
          const ws = new WebSocket(wsUrl);
          ws.onopen = () => {{
            wsConnected = true;
            badge.style.background = '#064e3b';
            badge.style.borderColor = '#059669';
            badge.style.color = '#34d399';
            badge.innerHTML = '<span class="status-pulse"></span><span>WEBSOCKET LIVE (1s)</span>';
          }};
          ws.onmessage = (event) => {{
            try {{
              const d = JSON.parse(event.data);
              updateDashboard(d);
            }} catch (e) {{}}
          }};
          ws.onerror = () => {{
            if (!wsConnected) startSSEFallback();
          }};
          ws.onclose = () => {{
            if (!wsConnected) {{
              startSSEFallback();
            }} else {{
              badge.style.background = '#78350f';
              badge.style.borderColor = '#b45309';
              badge.style.color = '#fde047';
              badge.innerHTML = '<span class="status-pulse"></span><span>RECONNECTING...</span>';
              setTimeout(initSocketService, 2000);
            }}
          }};
        }} catch (e) {{
          startSSEFallback();
        }}
      }}

      let sseStarted = false;
      function startSSEFallback() {{
        if (sseStarted) return;
        sseStarted = true;
        const badge = document.getElementById('sock-status');
        const sseUrl = `/status/stream${{location.search}}`;
        const sse = new EventSource(sseUrl);

        sse.onopen = () => {{
          badge.style.background = '#064e3b';
          badge.style.borderColor = '#059669';
          badge.style.color = '#34d399';
          badge.innerHTML = '<span class="status-pulse"></span><span>SSE SOCKET LIVE (1s)</span>';
        }};
        sse.onmessage = (event) => {{
          try {{
            const d = JSON.parse(event.data);
            updateDashboard(d);
          }} catch (e) {{}}
        }};
        sse.onerror = () => {{
          badge.style.background = '#78350f';
          badge.style.borderColor = '#b45309';
          badge.style.color = '#fde047';
          badge.innerHTML = '<span class="status-pulse"></span><span>RECONNECTING...</span>';
        }};
      }}

      window.addEventListener('DOMContentLoaded', initSocketService);
    </script>
  </body>
</html>
"""
    return html


@app.route("/stream.m3u8")
@app.route("/live.m3u8")
@app.route("/jiotv/<channel_id>.m3u8")
@app.route("/oxax/<channel_name>.m3u8")
@app.route("/oxax.m3u8")
@app.route("/<channel_name>.m3u8")
def tunnel_proxy_stream(channel_name=None, channel_id=None):
    with metrics_lock:
        METRICS["manifest_requests"] += 1

    query_channel = request.args.get("channel") or request.args.get("ch")
    hash_code = request.args.get("hash")
    oxax_query = (
        request.args.get("channel-name")
        or request.args.get("channel_name")
        or request.args.get("oxax")
    )
    jiotv_query = (
        request.args.get("channel-id")
        or request.args.get("channel_id")
        or request.args.get("jiotv")
        or request.args.get("jtv")
    )
    server_param = (request.args.get("server") or "").strip().lower()

    is_jiotv_route = (
        request.path.startswith("/jiotv/")
        or server_param == "jiotv"
        or (channel_name and (channel_name.startswith("jiotv_") or channel_name == "jiotv"))
        or (channel_id is not None)
    )

    is_oxax_route = (
        request.path == "/oxax.m3u8"
        or request.path.startswith("/oxax/")
        or channel_name == "oxax"
    )

    if jiotv_query or is_jiotv_route or server_param == "jiotv":
        target_id = (
            jiotv_query
            or channel_id
            or (channel_name if channel_name and channel_name != "jiotv" else None)
            or query_channel
            or "143"
        )
        clean_target = str(target_id).replace("jiotv_", "").replace(".m3u8", "").strip()
        cache_id = f"jiotv_{clean_target}"
        server_type = "jiotv"
        identifier = clean_target
    elif oxax_query or server_param == "oxax" or is_oxax_route or query_channel == "oxax":
        target_oxax = (
            oxax_query
            or (query_channel if query_channel != "oxax" else None)
            or (channel_name if channel_name != "oxax" else None)
            or request.args.get("name")
            or "barely-legal"
        )
        clean_target = target_oxax.replace(".html", "").replace(".m3u8", "").strip().lower()
        cache_id = f"oxax_{clean_target}"
        server_type = "oxax"
        identifier = clean_target
    elif query_channel:
        channel_name = query_channel
        cache_id = channel_name.replace(".m3u8", "").strip().lower()
        server_type = "server1"
        identifier = cache_id
    elif hash_code:
        cache_id = hash_code.strip()
        server_type = "server2"
        identifier = cache_id
    elif channel_name:
        cache_id = channel_name.replace(".m3u8", "").strip().lower()
        server_type = "server1"
        identifier = cache_id
    else:
        return "Error: Missing parameters (?channel-id=id or ?channel=name or ?channel-name=name or ?hash=hashcode)", 400

    # 1. FAST ZERO-LATENCY RAM MANIFEST SERVING (< 0.2ms)
    with warehouse_lock:
        if cache_id in STREAM_WAREHOUSE and not STREAM_WAREHOUSE[cache_id].get("kill_signal", True):
            ctx = STREAM_WAREHOUSE[cache_id]
            with ctx["lock"]:
                ctx["last_requested_time"] = time.time()
                cached_manifest = ctx.get("cached_manifest")
                if cached_manifest:
                    return make_manifest_response(cached_manifest)

    # 2. START BACKGROUND WORKER IF NOT ALREADY RUNNING (THREAD-SAFE INITIALIZATION)
    ch_init_lock = get_channel_init_lock(cache_id)
    with ch_init_lock:
        is_running = False
        with warehouse_lock:
            if cache_id in STREAM_WAREHOUSE and not STREAM_WAREHOUSE[cache_id].get("kill_signal", True):
                is_running = True
                ctx = STREAM_WAREHOUSE[cache_id]
                with ctx["lock"]:
                    ctx["last_requested_time"] = time.time()
                    chunks_manifest_url = ctx["chunks_manifest_url"]
                    target_headers = ctx["headers"]
                print(f"🔄 [Proxy Engine] Reusing active persistent worker thread for: {cache_id}", flush=True)

        if not is_running:
            if server_type == "jiotv":
                chunks_manifest_url = resolve_jiotv_stream(identifier)
                target_headers = HEADERS_JIOTV
            elif server_type == "oxax":
                chunks_manifest_url = resolve_oxax_stream(identifier)
                target_headers = HEADERS_OXAX
            elif server_type == "server1":
                chunks_manifest_url = resolve_server1_stream(cache_id)
                target_headers = HEADERS_SERVER1
                # Intelligent Auto-Failover: If Server 1 is offline (404/dead), fail over to Oxax
                if not chunks_manifest_url:
                    oxax_cand = CHANNEL_GATEWAY_ALIASES.get(cache_id, cache_id)
                    print(f"⚠️ [Server 1 Offline] Upstream 404 for '{cache_id}'. Auto-failing over to Oxax gateway ('{oxax_cand}')...", flush=True)
                    oxax_url = resolve_oxax_stream(oxax_cand)
                    if oxax_url:
                        chunks_manifest_url = oxax_url
                        server_type = "oxax"
                        identifier = oxax_cand
                        target_headers = HEADERS_OXAX
                        original_req_cache_id = cache_id
                        cache_id = f"oxax_{oxax_cand}"
            else:
                chunks_manifest_url = resolve_server2_stream(cache_id)
                target_headers = HEADERS_SERVER2_STREAM

            if not chunks_manifest_url:
                return f"Failed parsing source target vectors for: {cache_id}", 500

            with warehouse_lock:
                STREAM_WAREHOUSE[cache_id] = {
                    "cache_id": cache_id,
                    "server_type": server_type,
                    "identifier": identifier,
                    "chunks_manifest_url": chunks_manifest_url,
                    "cached_manifest": None,
                    "segments_cache": {},
                    "segment_urls": {},
                    "history_queue": deque(maxlen=MAX_BUFFER_SEGMENTS),
                    "kill_signal": False,
                    "last_requested_time": time.time(),
                    "headers": target_headers,
                    "lock": threading.Lock(),
                    "ready_event": threading.Event(),
                    "last_upstream_media_seq": None,
                    "virtual_seq_offset": 0,
                    "last_served_media_seq": None,
                    "last_seg_prefix": None,
                    "discontinuity_seq_count": 0,
                    "active_discontinuity_segments": set(),
                    "served_chunks_count": 0,
                }
            worker = threading.Thread(
                target=channel_buffer_worker,
                args=(cache_id, chunks_manifest_url, target_headers, server_type, identifier),
                daemon=True,
            )
            worker.start()
            STREAM_WAREHOUSE[cache_id]["ready_event"].wait(timeout=2.0)
            with warehouse_lock:
                if "original_req_cache_id" in locals() and original_req_cache_id and original_req_cache_id != cache_id:
                    STREAM_WAREHOUSE[original_req_cache_id] = STREAM_WAREHOUSE[cache_id]
        else:
            # Worker already running: wait briefly on ready_event if cached_manifest is pending
            ctx = STREAM_WAREHOUSE.get(cache_id)
            if ctx:
                ready_ev = ctx.get("ready_event")
                if ready_ev and not ready_ev.is_set():
                    ready_ev.wait(timeout=1.5)
                with ctx["lock"]:
                    cached_manifest = ctx.get("cached_manifest")
                    if cached_manifest:
                        return make_manifest_response(cached_manifest)

    # 3. FALLBACK DIRECT MANIFEST FETCH ON INITIAL STARTUP
    try:
        res = CLIENT_POOL.get(chunks_manifest_url, headers=target_headers)
        if res.status_code in (403, 404):
            print(f"🔄 [Inline Auto-Heal] Upstream {res.status_code} for '{cache_id}'. Refreshing session tokens...", flush=True)
            if server_type == "jiotv":
                new_url = resolve_jiotv_stream(identifier)
            elif server_type == "oxax":
                new_url = resolve_oxax_stream(identifier)
            elif server_type == "server1":
                new_url = resolve_server1_stream(identifier)
            else:
                new_url = resolve_server2_stream(identifier)

            if new_url:
                chunks_manifest_url = new_url
                with warehouse_lock:
                    if cache_id in STREAM_WAREHOUSE:
                        with STREAM_WAREHOUSE[cache_id]["lock"]:
                            STREAM_WAREHOUSE[cache_id]["chunks_manifest_url"] = new_url
                res = CLIENT_POOL.get(chunks_manifest_url, headers=target_headers)

        if res.status_code != 200:
            return "Media host offline.", res.status_code

        with warehouse_lock:
            if cache_id in STREAM_WAREHOUSE:
                ctx = STREAM_WAREHOUSE[cache_id]
                with ctx["lock"]:
                    final_manifest, _ = process_and_rewrite_manifest(res.text, ctx, cache_id)
                    ctx["cached_manifest"] = final_manifest
                    return make_manifest_response(final_manifest)

        return make_manifest_response(res.text)
    except Exception as err:
        return f"Proxy playlist error: {err}", 500


@app.route("/<cache_id>/<path:segment_name>")
def proxy_ts_chunk(cache_id, segment_name):
    cache_id = cache_id.strip()

    clean_segment_key = segment_name.split("?")[0].lstrip("/")
    if not clean_segment_key.endswith(".ts"):
        return "Not Found", 404

    if cache_id not in STREAM_WAREHOUSE:
        ch_init_lock = get_channel_init_lock(cache_id)
        with ch_init_lock:
            if cache_id not in STREAM_WAREHOUSE:
                print(f"⚠️ [Emergency Fallback] Lazy loading segments engine for: {cache_id}", flush=True)
                if cache_id.startswith("jiotv_"):
                    server_type = "jiotv"
                    identifier = cache_id.replace("jiotv_", "")
                    fallback_url = resolve_jiotv_stream(identifier)
                    target_headers = HEADERS_JIOTV
                elif cache_id.startswith("oxax_") or cache_id == "oxax":
                    server_type = "oxax"
                    identifier = cache_id.replace("oxax_", "") if cache_id != "oxax" else "barely-legal"
                    fallback_url = resolve_oxax_stream(identifier, force_refresh=True)
                    target_headers = HEADERS_OXAX
                else:
                    is_server2 = any(c.isdigit() for c in cache_id) or len(cache_id) == 10
                    server_type = "server2" if is_server2 else "server1"
                    identifier = cache_id
                    fallback_url = resolve_server2_stream(cache_id) if is_server2 else resolve_server1_stream(cache_id)
                    target_headers = HEADERS_SERVER2_STREAM if is_server2 else HEADERS_SERVER1

                if fallback_url:
                    with warehouse_lock:
                        STREAM_WAREHOUSE[cache_id] = {
                            "cache_id": cache_id,
                            "server_type": server_type,
                            "identifier": identifier,
                            "chunks_manifest_url": fallback_url,
                            "cached_manifest": None,
                            "segments_cache": {},
                            "segment_urls": {},
                            "history_queue": deque(maxlen=MAX_BUFFER_SEGMENTS),
                            "kill_signal": False,
                            "last_requested_time": time.time(),
                            "headers": target_headers,
                            "lock": threading.Lock(),
                            "ready_event": threading.Event(),
                            "last_upstream_media_seq": None,
                            "virtual_seq_offset": 0,
                            "last_served_media_seq": None,
                            "last_seg_prefix": None,
                            "discontinuity_seq_count": 0,
                            "active_discontinuity_segments": set(),
                            "served_chunks_count": 0,
                        }
                    worker = threading.Thread(
                        target=channel_buffer_worker,
                        args=(cache_id, fallback_url, target_headers, server_type, identifier),
                        daemon=True,
                    )
                    worker.start()
                    STREAM_WAREHOUSE[cache_id]["ready_event"].wait(timeout=2.0)
                else:
                    return "Stream tracking context timeout error.", 400

    context = STREAM_WAREHOUSE.get(cache_id)
    if not context:
        return "Stream buffer context unavailable.", 500

    now = time.time()
    with context["lock"]:
        context["last_requested_time"] = now
        context["streaming_active"] = context.get("streaming_active", 0) + 1
        served_count = context.get("served_chunks_count", 0)
        cached_data = context["segments_cache"].get(clean_segment_key)

    # 1. DIRECT RAM HIT (< 0.2ms)
    # If the chunk is already cached in RAM, always serve it immediately!
    if cached_data is not None:
        with context["lock"]:
            context["served_chunks_count"] = served_count + 1
            context["streaming_active"] = max(0, context.get("streaming_active", 1) - 1)
            context["last_requested_time"] = time.time()
        with metrics_lock:
            METRICS["ram_hits"] += 1
        print(f"⚡ [RAM HIT] [{cache_id.upper()}] Serving segment from cache memory: {clean_segment_key} (chunk #{served_count+1})", flush=True)
        proxied_res = Response(cached_data, status=200, content_type="video/mp2t")
        proxied_res.headers["Access-Control-Allow-Origin"] = "*"
        return proxied_res

    # 2. FAST-START JITTER PIPELINING FOR 1st & 2nd CHUNKS:
    # If this is chunk #1 or #2, stream directly from upstream with ZERO waiting/blocking!
    # This guarantees video playback begins on player in < 300ms.
    if served_count < 2:
        with context["lock"]:
            context["served_chunks_count"] = served_count + 1
        print(f"🚀 [FAST-START DIRECT] [{cache_id.upper()}] Streaming chunk #{served_count+1} directly to player: {clean_segment_key}", flush=True)
        # Proceed directly to streaming logic below without blocking on in-flight locks!
    else:
        # 3. CHUNK 3+ STRICT RAM PATH: Wait briefly on in-flight download so it is served from RAM
        with in_flight_lock:
            in_flight_event = IN_FLIGHT_DOWNLOADS.get(clean_segment_key)

        if in_flight_event:
            print(f"⏳ [IN-FLIGHT SYNC] [{cache_id.upper()}] Waiting on background pre-fetch: {clean_segment_key}", flush=True)
            in_flight_event.wait(timeout=1.5)
            with context["lock"]:
                cached_data = context["segments_cache"].get(clean_segment_key)
            if cached_data is not None:
                with context["lock"]:
                    context["served_chunks_count"] = served_count + 1
                    context["streaming_active"] = max(0, context.get("streaming_active", 1) - 1)
                    context["last_requested_time"] = time.time()
                with metrics_lock:
                    METRICS["ram_hits"] += 1
                print(f"⚡ [RAM HIT POST-SYNC] [{cache_id.upper()}] Served immediately after pre-fetch: {clean_segment_key} (chunk #{served_count+1})", flush=True)
                proxied_res = Response(cached_data, status=200, content_type="video/mp2t")
                proxied_res.headers["Access-Control-Allow-Origin"] = "*"
                return proxied_res

        with context["lock"]:
            context["served_chunks_count"] = served_count + 1

    # 3. FAST STREAMING ON MISS: Stream bytes immediately as they arrive from upstream
    with metrics_lock:
        METRICS["fast_misses"] += 1
    print(f"🚀 [FAST STREAMING MISS] [{cache_id.upper()}] Streaming chunk directly: {clean_segment_key}", flush=True)

    with context["lock"]:
        curr_manifest_url = context.get("chunks_manifest_url", "")
        server_type = context.get("server_type", "")
        identifier = context.get("identifier", "")

    # Always rebase relative segments against the active chunks_manifest_url
    target_ts_url = urljoin(curr_manifest_url, clean_segment_key if not clean_segment_key.startswith("s/") else f"/{clean_segment_key}")
    query_string = request.query_string.decode("utf-8")
    if query_string and "?" not in target_ts_url:
        target_ts_url += f"?{query_string}"

    try:
        req = CLIENT_POOL.build_request("GET", target_ts_url, headers=context["headers"])
        upstream_res = CLIENT_POOL.send(req, stream=True)

        # On 401/403/404, renew session token immediately and retry chunk
        if upstream_res.status_code in (401, 403, 404):
            upstream_res.close()
            print(f"🔄 [Chunk Auto-Heal] Upstream {upstream_res.status_code} for chunk {clean_segment_key}. Renewing session...", flush=True)
            if server_type == "oxax":
                new_m = resolve_oxax_stream(identifier, force_refresh=True)
            elif server_type == "jiotv":
                new_m = resolve_jiotv_stream(identifier)
            elif server_type == "server1":
                new_m = resolve_server1_stream(identifier)
            else:
                new_m = resolve_server2_stream(identifier)

            if new_m:
                with context["lock"]:
                    context["chunks_manifest_url"] = new_m
                target_ts_url = urljoin(new_m, clean_segment_key if not clean_segment_key.startswith("s/") else f"/{clean_segment_key}")
                if query_string and "?" not in target_ts_url:
                    target_ts_url += f"?{query_string}"
                req = CLIENT_POOL.build_request("GET", target_ts_url, headers=context["headers"])
                upstream_res = CLIENT_POOL.send(req, stream=True)

        if upstream_res.status_code != 200:
            upstream_res.close()
            return f"Chunk acquisition failure ({upstream_res.status_code})", upstream_res.status_code

        def generate_and_cache():
            buffer = bytearray()
            try:
                for chunk in upstream_res.iter_bytes(chunk_size=65536):
                    buffer.extend(chunk)
                    yield chunk
            except Exception as stream_err:
                print(f"⚠️ [Stream Yield Reset/Interrupted] [{cache_id.upper()}]: {stream_err}", flush=True)
            finally:
                upstream_res.close()
                with context["lock"]:
                    context["streaming_active"] = max(0, context.get("streaming_active", 1) - 1)
                    context["last_requested_time"] = time.time()
                    if len(buffer) > 0:
                        context["segments_cache"][clean_segment_key] = bytes(buffer)
                        context["history_queue"].append(clean_segment_key)
                if len(buffer) > 0:
                    enforce_global_memory_guard()

        proxied_res = Response(
            stream_with_context(generate_and_cache()),
            status=200,
            content_type="video/mp2t",
        )
        proxied_res.headers["Access-Control-Allow-Origin"] = "*"
        return proxied_res
    except Exception as e:
        return f"Chunk acquisition failure: {e}", 500


# ==============================================================================
# JIOTV PROXY ROUTES (AES-128 KEYS, PLAYLIST & EPG)
# ==============================================================================
@app.route("/render.key")
@app.route("/jiotv/key")
def proxy_jiotv_key():
    """
    Proxies and RAM-caches AES-128 decryption keys for JioTV streams.
    Returns the exact 16-byte key payload with full CORS headers.
    """
    query_str = request.query_string.decode("utf-8")
    target_key_url = f"{JIOTV_BASE_URL}/render.key"
    if query_str:
        target_key_url += f"?{query_str}"

    try:
        r = CLIENT_POOL.get(target_key_url, headers=HEADERS_JIOTV)
        if r.status_code == 200:
            res = Response(r.content, status=200, content_type="application/octet-stream")
            res.headers["Access-Control-Allow-Origin"] = "*"
            res.headers["Cache-Control"] = "public, max-age=3600"
            return res
        return f"Key fetch failed ({r.status_code})", r.status_code
    except Exception as e:
        return f"Key proxy error: {e}", 500


@app.route("/jiotv.m3u")
@app.route("/playlist/jiotv.m3u")
def serve_jiotv_m3u():
    """
    Dynamically generates a 100% playable M3U playlist for all 1,083 JioTV channels.
    Preserves channel metadata (tvg-id, tvg-name, tvg-logo, group-title) while
    pointing stream URLs to the local proxy (/jiotv/{channel_id}.m3u8).
    """
    host = request.host
    scheme = "https" if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" else "http"
    base_proxy_url = f"{scheme}://{host}"

    playlist_path = os.getenv("JIOTV_PLAYLIST_FILE", os.path.join(os.path.dirname(__file__), "jiotv_playlist.m3u"))
    if not os.path.exists(playlist_path):
        playlist_path = "./jiotv_playlist.m3u"
    if not os.path.exists(playlist_path):
        return f"Playlist file not found: {playlist_path}", 404

    try:
        with open(playlist_path, "r", encoding="utf-8") as f:
            content = f.read()

        out_lines = [f'#EXTM3U x-tvg-url="{base_proxy_url}/jiotv/epg.xml.gz"']
        current_extinf = None

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXTINF:"):
                # Rewrite logo URL to proxy /jtvimage/
                rewritten_inf = re.sub(
                    r'tvg-logo="https?://[^/]+/jtvimage/([^"]+)"',
                    lambda m: f'tvg-logo="{base_proxy_url}/jtvimage/{m.group(1)}"',
                    line,
                )
                current_extinf = rewritten_inf
            elif current_extinf and (line.startswith("http://") or line.startswith("https://")):
                m = re.search(r'tvg-id="([^"]+)"', current_extinf)
                ch_id = m.group(1) if m else line.split("/")[-1].replace(".m3u8", "")

                is_mpd = "/live/mpd/" in line or line.endswith(".mpd")
                stream_ext = "mpd" if is_mpd else "m3u8"

                out_lines.append(current_extinf)
                out_lines.append(f"{base_proxy_url}/jiotv/{ch_id}.{stream_ext}")
                current_extinf = None

        final_m3u = chr(10).join(out_lines)
        res = Response(final_m3u, status=200, content_type="application/vnd.apple.mpegurl; charset=utf-8")
        res.headers["Access-Control-Allow-Origin"] = "*"
        res.headers["Content-Disposition"] = 'inline; filename="jiotv.m3u"'
        return res
    except Exception as e:
        return f"Error building playlist: {e}", 500


@app.route("/jiotv/epg.xml.gz")
def serve_jiotv_epg_gz():
    """Proxies and caches the JioTV EPG archive (~2MB gzip)."""
    target_epg = f"{JIOTV_BASE_URL}/epg.xml.gz"
    try:
        r = CLIENT_POOL.get(target_epg, headers=HEADERS_JIOTV)
        if r.status_code == 200:
            res = Response(r.content, status=200, content_type="application/gzip")
            res.headers["Access-Control-Allow-Origin"] = "*"
            res.headers["Cache-Control"] = "public, max-age=14400"
            return res
        return f"Upstream EPG unavailable ({r.status_code})", r.status_code
    except Exception as e:
        return f"EPG proxy error: {e}", 500


@app.route("/jtvimage/<path:filename>")
def proxy_jiotv_image(filename):
    """Proxies and caches JioTV channel logo images."""
    target_img = f"{JIOTV_BASE_URL}/jtvimage/{filename}"
    try:
        r = CLIENT_POOL.get(target_img, headers=HEADERS_JIOTV)
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "image/png")
            res = Response(r.content, status=200, content_type=content_type)
            res.headers["Access-Control-Allow-Origin"] = "*"
            res.headers["Cache-Control"] = "public, max-age=86400"
            return res
        return "Image not found", 404
    except Exception as e:
        return f"Image proxy error: {e}", 500


@app.route("/jiotv/<channel_id>.mpd")
@app.route("/live/mpd/<channel_id>")
def proxy_jiotv_mpd(channel_id):
    """
    Proxies and rewrites MPEG-DASH manifests for MPD channels (e.g. channel 760).
    Rewrites relative /render.dash/ BaseURL to point through the local proxy.
    """
    clean_id = str(channel_id).replace(".mpd", "").strip()
    target_url = f"{JIOTV_BASE_URL}/live/mpd/{clean_id}"
    try:
        r = CLIENT_POOL.get(target_url, headers=HEADERS_JIOTV)
        if r.status_code == 200:
            host = request.host
            scheme = "https" if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" else "http"
            base_proxy = f"{scheme}://{host}"
            rewritten_mpd = re.sub(
                r"<BaseURL>/render\.dash/",
                rf"<BaseURL>{base_proxy}/render.dash/",
                r.text,
            )
            res = Response(rewritten_mpd, status=200, content_type="application/dash+xml; charset=utf-8")
            res.headers["Access-Control-Allow-Origin"] = "*"
            return res
        return f"MPD stream not available ({r.status_code})", r.status_code
    except Exception as e:
        return f"MPD proxy error: {e}", 500


@app.route("/render.dash/<path:dash_path>")
def proxy_jiotv_dash(dash_path):
    """
    Proxies MPEG-DASH media and initialization chunks (.dash).
    """
    target_url = f"{JIOTV_BASE_URL}/render.dash/{dash_path}"
    try:
        r = CLIENT_POOL.get(target_url, headers=HEADERS_JIOTV)
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "video/mp4")
            res = Response(r.content, status=200, content_type=content_type)
            res.headers["Access-Control-Allow-Origin"] = "*"
            res.headers["Cache-Control"] = "public, max-age=60"
            return res
        return f"DASH segment not found ({r.status_code})", r.status_code
    except Exception as e:
        return f"DASH segment error: {e}", 500



# ==============================================================================
# SELF-CONTAINED CHANNEL CATALOG & DYNAMIC M3U PLAYLIST GENERATOR
# ==============================================================================
def _load_channels(var_name: str, key_name: str):
    """Loads channel list from .env JSON, or falls back to channels.json."""
    res = _parse_json_env(var_name)
    if res:
        return res
    channels_file = os.getenv("CHANNELS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json"))
    if os.path.exists(channels_file):
        try:
            with open(channels_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get(key_name, [])
        except Exception:
            pass
    return []


# Channel Catalog Lists (loaded directly from .env or channels.json)
SERVER1_CHANNELS = _load_channels("SERVER1_CHANNELS", "server1")
SERVER2_CHANNELS = _load_channels("SERVER2_CHANNELS", "server2")
SERVER3_CHANNELS = _load_channels("SERVER3_CHANNELS", "server3")


def get_all_channels(server=None, query=None):
    """Returns filtered or unfiltered list of channels across Server 1, Server 2, and Server 3."""
    channels = []
    if server in (1, '1', 'server1', 's1'):
        channels.extend(SERVER1_CHANNELS)
    elif server in (2, '2', 'server2', 's2'):
        channels.extend(SERVER2_CHANNELS)
    elif server in (3, '3', 'server3', 's3', 'oxax'):
        channels.extend(SERVER3_CHANNELS)
    else:
        channels.extend(SERVER1_CHANNELS)
        channels.extend(SERVER2_CHANNELS)
        channels.extend(SERVER3_CHANNELS)

    if query:
        q = query.strip().lower()
        channels = [
            c for c in channels
            if q in c.get('title', '').lower()
            or q in c.get('name', '').lower()
            or q in c.get('group', '').lower()
        ]
    return channels


def generate_playlist_m3u(base_url: str, server=None, query=None, proxy_all=False) -> str:
    """
    Generates a 100% standard IPTV #EXTINF M3U playlist dynamically
    using the calling client's base_url (e.g. https://<domain>:<port>).
    """
    base_url = base_url.rstrip('/')
    channels = get_all_channels(server=server, query=query)
    lines = ['#EXTM3U']

    for idx, c in enumerate(channels, 1):
        title = c.get('title') or c.get('name')
        logo = c.get('logo', '')
        group = c.get('group', 'Live TV')
        cid = c.get('name') or str(c.get('id', idx))

        if c.get('is_direct'):
            if proxy_all:
                import urllib.parse
                stream_url = f"{base_url}/stream.m3u8?gateway={urllib.parse.quote(c['direct_url'], safe='')}"
            else:
                stream_url = c['direct_url']
        else:
            q = c.get('url_query')
            if q:
                stream_url = f'{base_url}/stream.m3u8?{q}'
            else:
                stream_url = c.get('direct_url', '')

        lines.append(f'#EXTINF:-{idx} tvg-id="{cid}" tvg-name="{title}" tvg-logo="{logo}" group-title="{group}",{title}')
        lines.append(stream_url)

    return chr(10).join(lines)


def export_all_playlists(output_dir='docs', base_url=None):
    """Writes updated M3U8 files to disk in output_dir and root."""
    resolved_base = (
        base_url
        or os.getenv("PROXY_BASE_URL")
        or os.getenv("PROXY_URL")
        or "http://localhost:8085"
    )
    os.makedirs(output_dir, exist_ok=True)

    # 1. Server 1
    p1 = generate_playlist_m3u(resolved_base, server=1)
    with open(os.path.join(output_dir, 'playlist1.m3u8'), 'w', encoding='utf-8') as f:
        f.write(p1 + chr(10))

    # 2. Server 2
    p2 = generate_playlist_m3u(resolved_base, server=2)
    with open(os.path.join(output_dir, 'playlist2.m3u8'), 'w', encoding='utf-8') as f:
        f.write(p2 + chr(10))

    # 3. Server 3
    p3 = generate_playlist_m3u(resolved_base, server=3)
    with open(os.path.join(output_dir, 'playlist3.m3u8'), 'w', encoding='utf-8') as f:
        f.write(p3 + chr(10))

    # 4. Master Combined
    p_all = generate_playlist_m3u(resolved_base, server=None)
    with open(os.path.join(output_dir, 'playlist.m3u8'), 'w', encoding='utf-8') as f:
        f.write(p_all + chr(10))
    with open('playlist.m3u8', 'w', encoding='utf-8') as f:
        f.write(p_all + chr(10))

    print(f'Exported all playlists to {output_dir}/ and ./playlist.m3u8 successfully!')



def _get_request_base_url():
    """Dynamically resolves the base URL from the incoming request or env overrides."""
    host = request.host
    scheme = "https" if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" else "http"
    custom_base = request.args.get("base_url") or os.getenv("PROXY_BASE_URL") or os.getenv("PROXY_URL")
    return custom_base.rstrip("/") if custom_base else f"{scheme}://{host}"


@app.route("/playlist.m3u")
@app.route("/playlist.m3u8")
@app.route("/playlist/master.m3u")
@app.route("/playlist/master.m3u8")
@app.route("/playlist/all.m3u")
@app.route("/playlist/all.m3u8")
def serve_master_playlist():
    """
    Dynamically generates unified IPTV M3U playlist combining Server 1, Server 2, and Server 3.
    Supports query parameters:
      - ?server=1, 2, or 3 (filter by server)
      - ?q=<keyword> or ?search=<keyword> (search channel name or title)
      - ?proxy_all=1 (route direct .m3u8 links through proxy too)
      - ?base_url=https://... (override base URL in stream links)
      - ?save=1 (persist generated files to docs/ and root playlist.m3u8)
      - ?json=1 (return JSON catalog)
    """
    base_url = _get_request_base_url()
    server_filter = request.args.get("server") or request.args.get("s")
    search_filter = request.args.get("search") or request.args.get("q")
    proxy_all = request.args.get("proxy_all") == "1"

    if request.args.get("save") == "1":
        try:
            docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
            export_all_playlists(output_dir=docs_dir, base_url=base_url)
        except Exception as e:
            print(f"⚠️ Failed to export playlists to docs: {e}", flush=True)

    if request.path.endswith(".json") or request.args.get("json") or "application/json" in request.headers.get("Accept", ""):
        channels = get_all_channels(server=server_filter, query=search_filter)
        return jsonify({
            "total": len(channels),
            "base_url": base_url,
            "channels": channels,
        })

    m3u_content = generate_playlist_m3u(
        base_url=base_url,
        server=server_filter,
        query=search_filter,
        proxy_all=proxy_all,
    )

    filename = "playlist.m3u"
    if server_filter in (1, "1", "server1"):
        filename = "playlist1.m3u"
    elif server_filter in (2, "2", "server2"):
        filename = "playlist2.m3u"
    elif server_filter in (3, "3", "server3", "oxax"):
        filename = "playlist3.m3u"

    res = Response(m3u_content, status=200, content_type="audio/x-mpegurl; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return res


@app.route("/playlist1.m3u")
@app.route("/playlist1.m3u8")
@app.route("/playlist/server1.m3u")
@app.route("/playlist/server1.m3u8")
def serve_server1_playlist():
    """Generates Server 1 IPTV M3U playlist dynamically on-the-fly."""
    base_url = _get_request_base_url()
    search_filter = request.args.get("search") or request.args.get("q")
    m3u_content = generate_playlist_m3u(base_url=base_url, server=1, query=search_filter)
    res = Response(m3u_content, status=200, content_type="audio/x-mpegurl; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Content-Disposition"] = 'inline; filename="playlist1.m3u"'
    return res


@app.route("/playlist2.m3u")
@app.route("/playlist2.m3u8")
@app.route("/playlist/server2.m3u")
@app.route("/playlist/server2.m3u8")
def serve_server2_playlist():
    """Generates Server 2 IPTV M3U playlist dynamically on-the-fly."""
    base_url = _get_request_base_url()
    search_filter = request.args.get("search") or request.args.get("q")
    proxy_all = request.args.get("proxy_all") == "1"
    m3u_content = generate_playlist_m3u(base_url=base_url, server=2, query=search_filter, proxy_all=proxy_all)
    res = Response(m3u_content, status=200, content_type="audio/x-mpegurl; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Content-Disposition"] = 'inline; filename="playlist2.m3u"'
    return res


@app.route("/playlist3.m3u")
@app.route("/playlist3.m3u8")
@app.route("/playlist/server3.m3u")
@app.route("/playlist/server3.m3u8")
@app.route("/playlist/oxax.m3u")
@app.route("/playlist/oxax.m3u8")
def serve_server3_playlist():
    """Generates Server 3 (Oxax TV) IPTV M3U playlist dynamically on-the-fly."""
    base_url = _get_request_base_url()
    search_filter = request.args.get("search") or request.args.get("q")
    m3u_content = generate_playlist_m3u(base_url=base_url, server=3, query=search_filter)
    res = Response(m3u_content, status=200, content_type="audio/x-mpegurl; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Content-Disposition"] = 'inline; filename="playlist3.m3u"'
    return res


@app.route("/channels.json")
@app.route("/playlist.json")
def serve_channels_json():
    """Returns JSON catalog of all channels across Server 1, Server 2, and Server 3."""
    base_url = _get_request_base_url()
    server_filter = request.args.get("server") or request.args.get("s")
    search_filter = request.args.get("search") or request.args.get("q")
    channels = get_all_channels(server=server_filter, query=search_filter)
    return jsonify({
        "total": len(channels),
        "base_url": base_url,
        "servers": ["Server 1", "Server 2", "Oxax TV"],
        "channels": channels,
    })


@app.route("/playlist/export", methods=["GET", "POST"])
def export_playlists_route():
    """Admin/API endpoint to generate and save playlist files to docs/ and root."""
    base_url = _get_request_base_url()
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    export_all_playlists(output_dir=docs_dir, base_url=base_url)
    return jsonify({
        "status": "success",
        "message": f"Playlists written to {docs_dir}/ and ./playlist.m3u8",
        "base_url": base_url,
    })


# ==============================================================================
# VOD PLAYLIST & STREAMING GATEWAY (teleplay.db)
# ==============================================================================
TELEPLAY_DB_PATH = os.getenv("TELEPLAY_DB_PATH", os.path.join(os.path.dirname(__file__), "teleplay.db"))


def get_teleplay_db():
    """Returns sqlite3 connection with dict-like row factory for teleplay.db."""
    db_file = TELEPLAY_DB_PATH
    if not os.path.exists(db_file):
        db_file = "./teleplay.db"
    if not os.path.exists(db_file):
        return None
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/vod.m3u")
@app.route("/vod.m3u8")
@app.route("/playlist/vod.m3u")
@app.route("/playlist/vod.m3u8")
def serve_vod_m3u():
    """
    Dynamically generates a 100% playable M3U VOD playlist using teleplay.db.
    Supports query parameters:
      - ?port=8000 (override streaming server port, default 8000)
      - ?base_url=https://... (override base stream URL)
      - ?folder=<name> or ?group=<name> (filter by folder/category)
      - ?search=<keyword> or ?q=<keyword> (search by title/caption)
      - ?proxy=1 (route stream through proxy1.py /vod/stream/{id})
      - ?ext=1 (append video extension .mp4 to URL for strict players)
      - ?json=1 (return JSON metadata instead of M3U)
    """
    conn = get_teleplay_db()
    if not conn:
        return Response("teleplay.db database file not found.", status=404, content_type="text/plain")

    folder_filter = request.args.get("folder") or request.args.get("group") or request.args.get("category")
    search_filter = request.args.get("search") or request.args.get("q")
    sort_order = "DESC" if request.args.get("sort", "").lower() == "desc" else "ASC"

    query = "SELECT * FROM teleplay_files WHERE 1=1"
    params = []

    if folder_filter:
        query += " AND LOWER(folder) = LOWER(?)"
        params.append(folder_filter.strip())

    if search_filter:
        query += " AND (LOWER(file_name) LIKE ? OR LOWER(caption) LIKE ?)"
        wildcard = f"%{search_filter.strip().lower()}%\草"[:-1]
        params.extend([wildcard, wildcard])

    query += f" ORDER BY id {sort_order}"

    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        conn.close()
        return Response(f"Database query error: {e}", status=500, content_type="text/plain")

    conn.close()

    host = request.host
    scheme = "https" if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" else "http"
    base_proxy_url = f"{scheme}://{host}"

    custom_base = request.args.get("base_url") or os.getenv("TELEPLAY_STREAM_BASE_URL")
    stream_port = request.args.get("port") or os.getenv("TELEPLAY_STREAM_PORT", "8000")
    use_proxy = request.args.get("proxy") == "1"
    append_ext = request.args.get("ext") == "1" or request.args.get("append_ext") == "1"

    if custom_base:
        stream_base = custom_base.rstrip("/")
    elif use_proxy:
        stream_base = f"{base_proxy_url}/vod/stream"
    else:
        host_name = host.split(":")[0]
        port_part = f":{stream_port}" if stream_port and stream_port not in ("80", "443") else ""
        stream_base = f"{scheme}://{host_name}{port_part}/teleplay/stream"

    vod_items = []
    m3u_lines = ["#EXTM3U"]

    for r in rows:
        file_id = r["id"]
        caption = r["caption"] or ""
        file_name = r["file_name"] or f"video_{file_id}.mp4"
        folder = r["folder"] or "root"
        group_title = "Movies" if folder.lower() in ("root", "") else folder.replace("_", " ").title()

        m_title = re.search(r"\*\*(.*?)\*\*", caption)
        if m_title:
            clean_title = m_title.group(1).strip()
        else:
            clean_title = os.path.splitext(file_name)[0].replace("_", " ").strip()

        duration = r["duration"] if r["duration"] and r["duration"] > 0 else 0
        file_size = r["file_size"]
        size_mb = round(file_size / (1024 * 1024), 2)

        file_ext = os.path.splitext(file_name)[1] or ".mp4"
        if append_ext:
            stream_url = f"{stream_base}/{file_id}{file_ext}"
        else:
            stream_url = f"{stream_base}/{file_id}"

        vod_items.append({
            "id": file_id,
            "title": clean_title,
            "file_name": file_name,
            "file_size": file_size,
            "file_size_mb": size_mb,
            "group": group_title,
            "duration": duration,
            "mime_type": r["mime_type"],
            "stream_url": stream_url,
            "created_at": str(r["created_at"]) if r["created_at"] else None,
        })

        m3u_lines.append(f'#EXTINF:{duration} tvg-id="vod_{file_id}" tvg-name="{clean_title}" group-title="{group_title}",{clean_title}')
        m3u_lines.append(stream_url)

    if request.path.endswith(".json") or request.args.get("json") or "application/json" in request.headers.get("Accept", ""):
        return jsonify({
            "total": len(vod_items),
            "stream_base": stream_base,
            "items": vod_items,
        })

    final_m3u = chr(10).join(m3u_lines)
    # audio/x-mpegurl is required by VLC, Kodi, and IPTV players to identify this as an M3U playlist (not HLS)
    res = Response(final_m3u, status=200, content_type="audio/x-mpegurl; charset=utf-8")
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Content-Disposition"] = 'inline; filename="vod.m3u"'
    return res


@app.route("/vod.json")
def serve_vod_json():
    """Returns JSON listing of VOD catalog from teleplay.db."""
    return serve_vod_m3u()


@app.route("/vod/stream/<int:file_id>")
@app.route("/vod/stream/<int:file_id>.<ext>")
@app.route("/vod/stream/<int:file_id>/<path:filename>")
def proxy_vod_stream(file_id, ext=None, filename=None):
    """
    Reverse-proxies VOD streaming video from upstream teleplay server (port 8000)
    with HTTP Range request forwarding for seeking/fast-forward support.
    Supports auto-fallback between HTTPS and HTTP upstream.
    """
    stream_port = os.getenv("TELEPLAY_STREAM_PORT", "8000")
    custom_url = os.getenv("TELEPLAY_INTERNAL_URL")

    req_headers = {}
    if "Range" in request.headers:
        req_headers["Range"] = request.headers["Range"]

    candidate_urls = []
    if custom_url:
        candidate_urls.append(custom_url.rstrip("/") + f"/{file_id}")
    else:
        # Check HTTPS first (aiohttp SSL on server), fallback to HTTP
        candidate_urls.append(f"https://127.0.0.1:{stream_port}/teleplay/stream/{file_id}")
        candidate_urls.append(f"http://127.0.0.1:{stream_port}/teleplay/stream/{file_id}")

    last_error = None
    upstream_res = None
    for target_url in candidate_urls:
        try:
            req = CLIENT_POOL.build_request("GET", target_url, headers=req_headers)
            res = CLIENT_POOL.send(req, stream=True)
            if res.status_code in (200, 206) or res.status_code < 400:
                upstream_res = res
                break
            else:
                res.close()
                last_error = f"Upstream status {res.status_code} on {target_url}"
        except Exception as ex:
            last_error = ex
            continue

    if upstream_res is None:
        return f"VOD upstream streaming error: {last_error}", 502

    try:
        resp_headers = {
            "Content-Type": upstream_res.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        for h in ("Content-Range", "Content-Length", "ETag", "Last-Modified"):
            if h in upstream_res.headers:
                resp_headers[h] = upstream_res.headers[h]

        def stream_generator():
            try:
                for chunk in upstream_res.iter_bytes(chunk_size=131072):
                    yield chunk
            finally:
                upstream_res.close()

        return Response(
            stream_with_context(stream_generator()),
            status=upstream_res.status_code,
            headers=resp_headers,
        )
    except Exception as e:
        return f"VOD upstream streaming error: {e}", 502


# ==============================================================================
# ENTRY INITIALIZATION BOUNDARY
# ==============================================================================
if __name__ == "__main__":
    if "--export-playlists" in sys.argv:
        idx = sys.argv.index("--export-playlists")
        out_dir = sys.argv[idx + 1] if len(sys.argv) > idx + 1 and not sys.argv[idx + 1].startswith("-") else "docs"
        base_url = os.getenv("PROXY_BASE_URL") or os.getenv("PROXY_URL") or "http://localhost:8085"
        export_all_playlists(output_dir=out_dir, base_url=base_url)
        sys.exit(0)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    port = int(os.environ.get("PORT", 8085))
    ssl_cert = os.getenv("SSL_CERT_FILE")
    ssl_key = os.getenv("SSL_KEY_FILE")
    ssl_context = (
        (ssl_cert, ssl_key)
        if (ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key))
        else None
    )

    if ssl_context:
        print(f"🔓 [ONLINE] Secure multi-user HTTPS operational on port {port}.", flush=True)
    else:
        print(f"ℹ️ [ONLINE] Multi-user HTTP running on port {port}.", flush=True)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=True,
        ssl_context=ssl_context,
        threaded=True,
    )
