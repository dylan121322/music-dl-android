"""Android-specific routes: stream URL proxy, cache, favorites, debug play."""
import hashlib
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests as _req
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from server_state import CONFIG_PATH, get_api
from server_models import FavoritesRequest, _song_to_dict
from utils import load_config

# CDN domains allowed for URL proxy and cache
ALLOWED_CDN_HOSTS = {
    "aqqmusic.tc.qq.com", "isure.stream.qqmusic.qq.com",
    "ws.stream.qqmusic.qq.com", "dl.stream.qqmusic.qq.com",
}


def _validate_cdn_url(url: str) -> str:
    """Validate URL domain against CDN allowlist. Returns hostname or raises."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL")
    if not host or not any(host == h or host.endswith("." + h) for h in ALLOWED_CDN_HOSTS):
        raise HTTPException(status_code=403, detail="URL domain not allowed")
    return host


def register(app):
    # ── Stream URL proxy (Android extends main /api/stream with url param) ──

    @app.get("/api/stream")
    def api_stream(path: str = "", url: str = ""):
        """Stream audio: local file (path param) or proxy external URL (url param)."""
        # Proxy external URL — only allowed CDN domains, no cookie forwarding
        if url:
            _validate_cdn_url(url)
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://y.qq.com",
                }
                resp = _req.get(url, headers=headers, stream=True, timeout=(5, 30))
                content_type = resp.headers.get("content-type", "audio/mpeg")
                content_length = resp.headers.get("content-length", "")
                if "mp4" in content_type or "m4a" in content_type.lower():
                    content_type = "audio/mp4"
                return StreamingResponse(
                    resp.iter_content(chunk_size=65536),
                    media_type=content_type,
                    status_code=200,
                    headers={
                        "Accept-Ranges": "none",
                        "Content-Length": content_length,
                        "Content-Type": content_type,
                    },
                )
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=404, detail="Cannot proxy URL")

        # Local file — validate path is within allowed directories
        if path:
            config = load_config(CONFIG_PATH)
            file_path = Path(path).resolve()
            allowed_dirs = [
                Path(config.get("download_dir", str(Path.home() / "Music"))).resolve(),
                Path(tempfile.gettempdir()).resolve() / "musicdl_cache",
            ]
            if not any(file_path.is_relative_to(d) for d in allowed_dirs):
                raise HTTPException(status_code=403, detail="Access denied")
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            return FileResponse(file_path, media_type="audio/mpeg")

        raise HTTPException(status_code=400, detail="Missing path or url param")

    # ── Cache endpoint (Android-specific) ──

    @app.get("/api/cache")
    def api_cache(url: str):
        """Download external URL to local cache and return local path for playback."""
        _validate_cdn_url(url)

        cache_dir = Path(tempfile.gettempdir()) / "musicdl_cache"
        cache_dir.mkdir(exist_ok=True)

        cache_key = hashlib.md5(url.encode()).hexdigest()[:12]
        cache_file = cache_dir / f"{cache_key}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 1024:
            return {"path": str(cache_file), "cached": True}

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://y.qq.com",
            }
            resp = _req.get(url, headers=headers, timeout=30)
            ct = resp.headers.get("content-type", "")
            if not any(t in ct for t in ("audio/", "video/mp4", "application/octet-stream")):
                raise HTTPException(status_code=400, detail="URL does not return audio content")
            with open(cache_file, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            return {"path": str(cache_file), "cached": False, "size": cache_file.stat().st_size}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="Cannot cache URL")

    # ── Favorites endpoint (Android-specific) ──

    @app.post("/api/favorites")
    def api_favorites(body: FavoritesRequest):
        api = get_api()
        if not api.g_tk:
            raise HTTPException(status_code=401, detail="Not logged in")
        songs = api.get_fav_songs(page=body.page, size=body.size)
        return {"songs": [_song_to_dict(s) for s in songs]}

    # ── Debug play endpoint (Android-specific) ──

    @app.get("/debug/play")
    def debug_play():
        api = get_api()
        songs = api.search("晴天", limit=3)
        results = []
        for song in songs[:2]:
            for q in ["320kbps"]:
                url = api.get_song_url(song.mid, q)
                if not url:
                    continue
                try:
                    r = _req.get(url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://y.qq.com",
                        "Cookie": api.session.headers.get("Cookie", ""),
                    }, timeout=15, stream=True)
                    size = 0
                    header = b""
                    for chunk in r.iter_content(65536):
                        header = chunk[:16]
                        size += len(chunk)
                        if size > 65536:
                            break
                    hexhdr = header.hex() if header else "none"
                    results.append({
                        "title": song.title, "q": q,
                        "status": r.status_code,
                        "type": r.headers.get("content-type", "?"),
                        "size": size,
                        "header": hexhdr,
                        "url": url[:120],
                    })
                    break
                except Exception as e:
                    results.append({"title": song.title, "q": q, "err": str(e)[:80]})
        return {"logged_in": bool(api.g_tk), "uin": api.uin, "results": results}
