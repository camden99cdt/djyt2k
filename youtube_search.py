import json
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class SearchResult:
    title: str
    url: str
    duration: str
    published: str
    thumbnail_bytes: bytes | None


def fetch_search_results(query: str) -> list[SearchResult]:
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--extractor-args",
        "youtubetab:approximate_date",
        f"ytsearch5:{query}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except Exception:
        return []

    results: list[SearchResult] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        url = data.get("webpage_url") or data.get("url")
        title = data.get("title") or "Untitled"
        duration = format_duration_from_seconds(data.get("duration"))
        published = format_time_ago(data)
        thumb_url = select_thumbnail_url(data)
        thumb_bytes = fetch_thumbnail_bytes(thumb_url) if thumb_url else None

        if url:
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    duration=duration,
                    published=published,
                    thumbnail_bytes=thumb_bytes,
                )
            )

        if len(results) >= 5:
            break

    return results


def select_thumbnail_url(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None

    thumbs = data.get("thumbnails")
    if isinstance(thumbs, list):
        for entry in thumbs:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url") or entry.get("thumbnail")
            if url:
                return url

    thumb_url = data.get("thumbnail")
    if thumb_url:
        return thumb_url

    return None


def format_duration_from_seconds(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_time_ago(data: dict) -> str:
    upload_date = data.get("upload_date") or data.get("release_date")
    timestamp = data.get("timestamp") or data.get("release_timestamp")
    dt: datetime | None = None
    if upload_date:
        try:
            dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    elif timestamp:
        try:
            dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            dt = None

    if not dt:
        return ""

    delta = datetime.now(tz=timezone.utc) - dt
    days = delta.days
    if days < 1:
        hours = delta.seconds // 3600
        if hours:
            return f"{hours}h ago"
        minutes = max(1, delta.seconds // 60)
        return f"{minutes}m ago"
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = months // 12
    return f"{years}y ago"


def fetch_thumbnail_bytes(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None
