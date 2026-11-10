from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

if sys.version_info < (3, 10):
    raise SystemExit("Pulse requires Python 3.10 or higher.")

try:
    import requests
    import yt_dlp
    from ytmusicapi import YTMusic
except ImportError:
    raise SystemExit("Install dependencies with: python -m pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
# Deployment mode: set PULSE_ALLOWED_ORIGINS to the public URL(s) of the hosted
# index.html (comma-separated). Empty = local-only mode, unchanged behavior.
ALLOWED_ORIGINS = {value.strip().rstrip("/") for value in os.environ.get("PULSE_ALLOWED_ORIGINS", "").split(",") if value.strip()}
DEPLOYED = bool(ALLOWED_ORIGINS)
# Optional signed-in YouTube session for hosts whose IPs YouTube bot-checks.
# Render exposes Secret Files at /etc/secrets/<name>, so paste a burner account's
# cookies.txt there as "pulse-yt-cookies.txt". The env var is the fallback for
# other hosts. Neither form is ever committed or served.
COOKIE_FILE = None
SECRET_COOKIES = Path("/etc/secrets/pulse-yt-cookies.txt")
if SECRET_COOKIES.is_file():
    COOKIE_FILE = SECRET_COOKIES
elif os.environ.get("PULSE_YT_COOKIES", "").strip():
    COOKIE_FILE = ROOT / ".pulse-yt-cookies.txt"
    COOKIE_FILE.write_text(os.environ["PULSE_YT_COOKIES"], encoding="utf-8")
POT_URL = os.environ.get("PULSE_POT_URL", "").strip()
CLIENTS = threading.local()
CACHE = {}
CACHE_LOCK = threading.Lock()
COLORS = ("#ff5c38", "#8b5cf6", "#35b8b0", "#f0a54a", "#c94f6d", "#4f8ff7")
FILTERS = {"songs", "videos", "albums", "artists", "playlists", "community_playlists", "featured_playlists"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
BROWSE_ID = re.compile(r"[A-Za-z0-9_-]{3,160}\Z")


class MusicSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", (5, 15))
        return super().request(*args, **kwargs)


def music():
    if not hasattr(CLIENTS, "music"):
        CLIENTS.music = YTMusic(requests_session=MusicSession(), language="en", location="US")
    return CLIENTS.music


def cached(key, load, ttl=600):
    now = time.monotonic()
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = load()
    with CACHE_LOCK:
        for old in [key for key, value in CACHE.items() if value[0] <= now]:
            CACHE.pop(old, None)
        if len(CACHE) >= 256:
            CACHE.pop(next(iter(CACHE)))
        CACHE[key] = (time.monotonic() + ttl, result)
    return result


def color(text):
    value = 0
    for char in text:
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return COLORS[value % len(COLORS)]


def thumbnail(raw):
    values = raw.get("thumbnails") or raw.get("thumbnail") or []
    if isinstance(values, dict):
        values = [values]
    candidates = [value for value in values if isinstance(value, dict)]
    if not candidates:
        return ""
    best = max(candidates, key=lambda value: (value.get("width") or 0) * (value.get("height") or 0))
    url = str(best.get("url") or "")
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith("https://") else ""


def duration(value):
    try:
        total = 0
        for part in str(value or 0).split(":"):
            total = total * 60 + int(float(part))
        return max(0, total)
    except (ValueError, TypeError):
        return 0


def people(raw):
    values = raw.get("artists") or raw.get("author") or raw.get("artist") or []
    if isinstance(values, str):
        return values, ""
    if isinstance(values, dict):
        values = [values]
    names = [str(value.get("name") or "") for value in values if isinstance(value, dict)]
    artist_id = next((value.get("id") for value in values if isinstance(value, dict) and value.get("id")), "")
    return ", ".join(filter(None, names)) or "Unknown artist", artist_id


def track(raw, art="", album=None, artist=None):
    video_id = raw.get("videoId")
    if not video_id or not VIDEO_ID.fullmatch(str(video_id)) or raw.get("isAvailable") is False:
        return None
    artist_name, artist_id = people(raw)
    if artist and not raw.get("artists"):
        artist_name, artist_id = artist
    release = raw.get("album") or album or {}
    if isinstance(release, str):
        release = {"name": release}
    title = str(raw.get("title") or "Untitled")
    album_name = str(release.get("name") or "Single")
    video_type = str(raw.get("videoType") or "")
    return {
        "id": video_id, "videoId": video_id, "title": title,
        "artist": artist_name, "artistId": artist_id,
        "album": album_name, "albumId": release.get("id") or "",
        "year": str(raw.get("year") or ""),
        "dur": duration(raw.get("duration_seconds") or raw.get("duration") or raw.get("length") or raw.get("lengthSeconds")),
        "art": thumbnail(raw) or art, "color": color(artist_name + album_name),
        "views": str(raw.get("views") or ""), "isExplicit": bool(raw.get("isExplicit")),
        "isAvailable": True,
        "isVideo": raw.get("resultType") == "video" or video_type in {"MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_UGC"},
        "videoType": video_type,
    }


def album(raw):
    browse_id = raw.get("browseId") or raw.get("albumId")
    if not browse_id:
        return None
    name, artist_id = people(raw)
    title = str(raw.get("title") or "Untitled album")
    return {
        "id": browse_id, "title": title, "artist": name, "artistId": artist_id,
        "year": str(raw.get("year") or ""), "type": raw.get("type") or "Album",
        "art": thumbnail(raw), "color": color(name + title),
        "desc": str(raw.get("description") or ""),
        "trackCount": duration(raw.get("trackCount") or 0), "loaded": False,
    }


def artist(raw):
    browse_id = raw.get("browseId") or raw.get("channelId") or raw.get("id")
    if not browse_id:
        return None
    name = str(raw.get("artist") or raw.get("name") or raw.get("title") or "Unknown artist")
    return {
        "id": browse_id, "name": name, "art": thumbnail(raw), "color": color(name),
        "subscribers": str(raw.get("subscribers") or ""),
        "description": str(raw.get("description") or ""),
        "songs": [], "albums": [], "loaded": False,
    }


def playlist(raw):
    playlist_id = str(raw.get("playlistId") or raw.get("browseId") or "")
    if playlist_id.startswith("VL"):
        playlist_id = playlist_id[2:]
    if not playlist_id:
        return None
    title = str(raw.get("title") or "Untitled playlist")
    author, _ = people({"author": raw.get("author") or "YouTube Music"})
    return {
        "id": playlist_id, "browseId": "VL" + playlist_id, "title": title, "name": title,
        "author": author, "art": thumbnail(raw), "color": color(author + title),
        "trackCount": duration(raw.get("trackCount") or raw.get("itemCount") or raw.get("count") or 0),
        "description": str(raw.get("description") or ""), "tracks": [], "loaded": False,
    }


def unique(items):
    output = {}
    for item in items:
        if item:
            output.setdefault(item["id"], item)
    return list(output.values())


def search(query, filter_name, limit, exact):
    client = music()
    if filter_name:
        normalize = track if filter_name in {"songs", "videos"} else album if filter_name == "albums" else artist if filter_name == "artists" else playlist
        results = client.search(query, filter=filter_name, limit=limit, ignore_spelling=exact)
        return {"query": query, "filter": filter_name, "items": unique(normalize(raw) for raw in results)[:limit]}
    results = client.search(query, limit=limit, ignore_spelling=exact)
    songs = client.search(query, filter="songs", limit=limit, ignore_spelling=exact)
    output = {"query": query, "songs": unique(track(raw) for raw in songs)[:limit], "videos": [], "albums": [], "artists": [], "playlists": [], "top": None}
    for raw in results:
        kind = raw.get("resultType")
        if kind in {"song", "video"}:
            item = track(raw)
            target, top_kind = ("videos" if kind == "video" else "songs"), "track"
        elif kind == "album":
            item, target, top_kind = album(raw), "albums", "album"
        elif kind == "artist":
            item, target, top_kind = artist(raw), "artists", "artist"
        elif kind == "playlist":
            item, target, top_kind = playlist(raw), "playlists", "playlist"
        else:
            continue
        if item:
            output[target].append(item)
            if raw.get("category") == "Top result" and output["top"] is None:
                output["top"] = {"kind": top_kind, "item": item}
    for target in ("songs", "videos", "albums", "artists", "playlists"):
        output[target] = unique(output[target])[:limit]
    if output["top"] is None:
        for target, kind in (("songs", "track"), ("videos", "track"), ("albums", "album"), ("artists", "artist"), ("playlists", "playlist")):
            if output[target]:
                output["top"] = {"kind": kind, "item": output[target][0]}
                break
    return output


def get_album(browse_id):
    client = music()
    if browse_id.startswith("OLAK"):
        browse_id = client.get_album_browse_id(browse_id)
    raw = client.get_album(browse_id)
    result = album({**raw, "browseId": browse_id})
    name, artist_id = people(raw)
    release = {"name": result["title"], "id": browse_id}
    result["tracks"] = unique(track(value, result["art"], release, (name, artist_id)) for value in raw.get("tracks") or [])
    for value in result["tracks"]:
        value["year"] = str(raw.get("year") or value["year"])
    result.update(loaded=True, trackCount=len(result["tracks"]), duration=sum(value["dur"] for value in result["tracks"]))
    return result


def get_artist(browse_id):
    client = music()
    raw = client.get_artist(browse_id)
    result = artist({**raw, "browseId": browse_id})
    songs = []
    releases = []
    for key in ("songs", "videos"):
        section = raw.get(key) or {}
        songs.extend(track(value, result["art"], artist=(result["name"], browse_id)) for value in section.get("results") or [])
    for key in ("albums", "singles"):
        section = raw.get(key) or {}
        values = section.get("results") or []
        if section.get("browseId") and section.get("params"):
            values = client.get_artist_albums(section["browseId"], section["params"])
        for value in values:
            release = album(value)
            if release:
                release["artist"], release["artistId"] = result["name"], browse_id
                releases.append(release)
    result.update(songs=unique(songs), albums=unique(releases), loaded=True)
    return result


def get_playlist(playlist_id):
    raw = music().get_playlist(playlist_id, limit=None)
    result = playlist({**raw, "playlistId": playlist_id})
    result["tracks"] = unique(track(value, result["art"]) for value in raw.get("tracks") or [])
    result.update(loaded=True, trackCount=len(result["tracks"]), duration=sum(value["dur"] for value in result["tracks"]))
    return result


def get_home():
    rows = music().get_home(limit=12)
    songs, albums, artists, playlists, sections = [], [], [], [], []
    for row in rows:
        items = []
        for raw in row.get("contents") or []:
            browse_id = str(raw.get("browseId") or "")
            if raw.get("videoId"):
                kind, value = "track", track(raw)
                songs.append(value)
            elif browse_id.startswith("MPRE") or raw.get("type") in {"Album", "Single", "EP"}:
                kind, value = "album", album(raw)
                albums.append(value)
            elif browse_id.startswith("UC"):
                kind, value = "artist", artist(raw)
                artists.append(value)
            elif raw.get("playlistId") or browse_id.startswith("VL"):
                kind, value = "playlist", playlist(raw)
                playlists.append(value)
            else:
                continue
            if value:
                items.append({"kind": kind, "item": value})
        if items:
            sections.append({"title": row.get("title") or "YouTube Music", "items": items})
    songs = unique(songs)
    return {
        "featured": songs[0] if songs else None, "trending": songs[:24],
        "albums": unique(albums)[:20], "artists": unique(artists)[:12],
        "playlists": unique(playlists)[:24], "sections": sections,
        "generatedAt": int(time.time()),
    }


def get_watch(video_id, playlist_id, limit, radio, shuffle):
    raw = music().get_watch_playlist(videoId=video_id or None, playlistId=playlist_id or None, limit=limit, radio=radio, shuffle=shuffle)
    return {
        "tracks": unique(track(value) for value in raw.get("tracks") or [])[:limit],
        "playlistId": raw.get("playlistId"), "lyrics": raw.get("lyrics"), "related": raw.get("related"),
    }


AUDIO_MIME = {"m4a": "audio/mp4", "webm": "audio/webm", "mp3": "audio/mpeg", "ogg": "audio/ogg", "opus": "audio/ogg"}


def resolve_audio(video_id):
    options = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 15,
        "extractor_args": {"youtube": {"player_client": ["web_embedded", "tv", "visionos"]}},
    }
    if POT_URL:
        options["js_runtimes"] = {"node": {}}
        # A PO token alone does not clear an IP-wide bot check; the signed-in web
        # client is what carries the session, so it joins the list once cookies exist.
        clients = ["web", "mweb", "tv"] if COOKIE_FILE else ["mweb", "tv"]
        options["extractor_args"] = {
            "youtube": {"player_client": clients, "fetch_pot": ["always"]},
            "youtubepot-bgutilhttp": {"base_url": [POT_URL]},
        }
    if COOKIE_FILE:
        options["cookiefile"] = str(COOKIE_FILE)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
    formats = [
        f for f in info.get("formats") or []
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and f.get("url")
        and f.get("protocol") in ("https", "http")
    ]
    if not formats:
        raise ValueError("No audio-only stream for this video")
    m4a = [f for f in formats if f.get("ext") == "m4a"]
    chosen = max(m4a or formats, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    return {
        "url": chosen["url"],
        "headers": chosen.get("http_headers") or info.get("http_headers") or {},
        "mime": AUDIO_MIME.get(chosen.get("ext"), "application/octet-stream"),
        "length": chosen.get("filesize") or chosen.get("filesize_approx"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cors_origin = None

    def send_cors(self, preflight=False):
        if not self.cors_origin:
            return
        self.send_header("Access-Control-Allow-Origin", self.cors_origin)
        self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Accept, Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        else:
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

    def send(self, body, status=200, content_type="application/json; charset=utf-8", head=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_cors()
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.cors_origin = None
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.cors_origin = origin
            self.send_response(204)
            self.send_cors(preflight=True)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send({"error": "Cross-origin requests are not allowed"}, 403)

    def handle_request(self, head=False):
        self.cors_origin = None
        try:
            host = self.headers.get("Host", "")
            hostname = (urlsplit("http://" + host).hostname or "").lower()
            # Local mode guards against DNS rebinding; a deployed server is
            # meant to be reached through its public hostname.
            if not DEPLOYED and hostname not in {"localhost", "127.0.0.1", "::1", self.server.server_address[0]}:
                self.send({"error": "Invalid host"}, 403, head=head)
                return
            origin = self.headers.get("Origin", "")
            if origin:
                if origin in ALLOWED_ORIGINS:
                    self.cors_origin = origin
                else:
                    source = urlsplit(origin)
                    if source.scheme not in {"http", "https"} or source.netloc.lower() != host.lower():
                        self.send({"error": "Cross-origin requests are not allowed"}, 403, head=head)
                        return
            parsed = urlsplit(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send((ROOT / "index.html").read_bytes(), content_type="text/html; charset=utf-8", head=head)
            elif parsed.path == "/favicon.ico":
                self.send(b"", status=204, head=head)
            elif parsed.path.startswith("/api/stream/"):
                self.stream_audio(unquote(parsed.path[len("/api/stream/"):]), head=head)
            elif parsed.path.startswith("/api/"):
                if head:
                    self.send({"error": "Use GET for API requests"}, 405, head=True)
                else:
                    self.route(parsed.path, parse_qs(parsed.query, keep_blank_values=True))
            else:
                self.send({"error": "Not found"}, 404, head=head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.log_error("Upstream error: %s", error)
            self.send({"error": "YouTube Music could not complete this request. Try again."}, 502, head=head)

    def stream_audio(self, video_id, head=False):
        if not VIDEO_ID.fullmatch(video_id):
            self.send({"error": "Invalid videoId"}, 400, head=head)
            return
        try:
            info = cached(("stream", video_id), lambda: resolve_audio(video_id), 600)
        except Exception as error:
            self.log_error("Stream resolve error: %s", error)
            self.send({"error": "This song can't be streamed right now. Try another song.", "detail": str(error)[:300]}, 502, head=head)
            return
        if head:
            self.send_response(200)
            self.send_header("Content-Type", info["mime"])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(info.get("length") or 0))
            self.send_header("Cache-Control", "no-store")
            self.send_cors()
            self.end_headers()
            return
        range_header = self.headers.get("Range", "").strip()
        if range_header and not re.fullmatch(r"bytes=\d*-\d*", range_header):
            range_header = ""
        for attempt in (0, 1):
            headers = dict(info["headers"])
            if range_header:
                headers["Range"] = range_header
            try:
                upstream = requests.get(info["url"], headers=headers, stream=True, timeout=(5, 30))
            except requests.RequestException as error:
                self.log_error("Stream fetch error: %s", error)
                self.send({"error": "The audio stream could not be reached. Try again."}, 502)
                return
            # Signed stream URLs can expire or be rejected; resolve once more.
            if upstream.status_code in (403, 410) and attempt == 0:
                upstream.close()
                with CACHE_LOCK:
                    CACHE.pop(("stream", video_id), None)
                try:
                    info = cached(("stream", video_id), lambda: resolve_audio(video_id), 600)
                except Exception as error:
                    self.log_error("Stream resolve error: %s", error)
                    self.send({"error": "This song can't be streamed right now. Try another song.", "detail": str(error)[:300]}, 502)
                    return
                continue
            break
        with upstream:
            if upstream.status_code not in (200, 206):
                self.send({"error": "This song can't be streamed right now. Try another song."}, 502)
                return
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", info["mime"])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            content_range = upstream.headers.get("Content-Range")
            if content_range:
                self.send_header("Content-Range", content_range)
            self.send_cors()
            self.end_headers()
            try:
                for chunk in upstream.iter_content(65536):
                    self.wfile.write(chunk)
            except requests.RequestException:
                pass

    def route(self, path, params):
        def param(name, default=""):
            return (params.get(name) or [default])[0].strip()

        def limit(default=24):
            try:
                value = int(param("limit", str(default)))
            except ValueError:
                raise ValueError("Limit must be an integer between 1 and 50")
            if not 1 <= value <= 50:
                raise ValueError("Limit must be between 1 and 50")
            return value

        def boolean(name, default=False):
            value = param(name, "true" if default else "false").lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(name + " must be true or false")
            return value in {"true", "1"}

        try:
            if path == "/api/health":
                self.send({"ok": True, "service": "pulse-ytmusicapi", "python": sys.version.split()[0]})
                return
            if path == "/api/home":
                self.send(cached("home", get_home, 600))
                return
            if path in {"/api/search", "/api/suggestions"}:
                query = param("q")
                if len(query) > 300:
                    raise ValueError("Search query must be at most 300 characters")
                if path == "/api/suggestions":
                    self.send(cached(("suggestions", query), lambda: {"items": music().get_search_suggestions(query)}, 120) if query else {"items": []})
                    return
                if not query:
                    raise ValueError("Enter a search query")
                filter_name = param("filter")
                if filter_name and filter_name not in FILTERS:
                    raise ValueError("Unsupported music search filter")
                count, exact = limit(), boolean("ignore_spelling")
                self.send(cached(("search", query, filter_name, count, exact), lambda: search(query, filter_name, count, exact)))
                return
            if path == "/api/watch":
                video_id, playlist_id = param("videoId"), param("playlistId")
                if not video_id and not playlist_id:
                    raise ValueError("A videoId or playlistId is required")
                if video_id and not VIDEO_ID.fullmatch(video_id):
                    raise ValueError("Invalid videoId")
                if playlist_id and not BROWSE_ID.fullmatch(playlist_id):
                    raise ValueError("Invalid playlistId")
                count, radio, shuffle = limit(25), boolean("radio"), boolean("shuffle")
                self.send(get_watch(video_id, playlist_id, count, radio, shuffle))
                return
            match = re.fullmatch(r"/api/(album|artist|playlist)/([^/]+)", path)
            if match:
                kind, identifier = match.groups()
                identifier = unquote(identifier)
                if not BROWSE_ID.fullmatch(identifier):
                    raise ValueError("Invalid music identifier")
                load = {"album": get_album, "artist": get_artist, "playlist": get_playlist}[kind]
                self.send(cached((kind, identifier), lambda: load(identifier), 900))
                return
            self.send({"error": "Unknown API endpoint"}, 404)
        except ValueError as error:
            self.send({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="Pulse: single-file UI with ytmusicapi (Python 3.10+)")
    parser.add_argument("--host", default=os.environ.get("PULSE_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    if not (ROOT / "index.html").is_file():
        raise SystemExit("Keep index.html next to server.py.")
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(f"Pulse is running at http://{args.host}:{args.port}", flush=True)
        if ALLOWED_ORIGINS:
            print("Allowed frontend origins: " + ", ".join(sorted(ALLOWED_ORIGINS)), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
