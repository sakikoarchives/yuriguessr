#!/usr/bin/env python3
"""Build a self-contained Artist Guessr index.html for GitHub Pages.

The browser never contacts Danbooru. This script runs in GitHub Actions,
fetches a fresh safe snapshot server-side, embeds image bytes as data URLs,
and writes dist/index.html.
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "index.template.html"
DIST_DIR = ROOT / "dist"
OUTPUT_PATH = DIST_DIR / "index.html"

API_BASE = "https://danbooru.donmai.us"
RATING_TAG = "yuri"
POOLS = [
    ("genshin_impact", "Genshin Impact"),
    ("zenless_zone_zero", "Zenless Zone Zero"),
    ("honkai_(series)", "Honkai (series)"),
]

TARGET_PER_POOL = int(os.getenv("TARGET_PER_POOL", "18"))
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "100"))
MAX_PAGE = int(os.getenv("MAX_PAGE", "14"))
MAX_API_ATTEMPTS = int(os.getenv("MAX_API_ATTEMPTS", "10"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "500000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
MIN_UNIQUE_ARTISTS = 4
VARIANT_PRIORITY = ("720x720", "360x360", "180x180")
USER_AGENT = os.getenv(
    "ARTIST_GUESSR_USER_AGENT",
    "ArtistGuessr/1.3 (+https://github.com/; GitHub Actions snapshot builder)",
)


def log(message: str) -> None:
    print(message, flush=True)


def request_bytes(url: str, *, accept: str, referer: str | None = None, max_bytes: int | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        content_type = response.headers.get_content_type() or "application/octet-stream"
        if max_bytes is None:
            data = response.read()
        else:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} bytes")
        return data, content_type


def request_json(url: str) -> Any:
    data, _ = request_bytes(url, accept="application/json")
    return json.loads(data.decode("utf-8"))


def retry_json(url: str, label: str) -> Any:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            return request_json(url)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last = exc
            log(f"  {label}: attempt {attempt}/3 failed: {exc}")
            time.sleep(1.2 * attempt)
    raise RuntimeError(f"{label} failed after retries: {last}")


def single_artist(post: dict[str, Any]) -> str | None:
    artists = str(post.get("tag_string_artist") or "").strip().split()
    return artists[0] if len(artists) == 1 else None


def media_variants(post: dict[str, Any]) -> list[tuple[str, str]]:
    media_asset = post.get("media_asset") or {}
    variants = media_asset.get("variants") or []
    by_type: dict[str, str] = {}
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        vtype = str(variant.get("type") or "")
        raw_url = variant.get("url")
        if vtype and raw_url:
            by_type[vtype] = urljoin(API_BASE + "/", str(raw_url))

    result: list[tuple[str, str]] = []
    for vtype in VARIANT_PRIORITY:
        if vtype in by_type:
            result.append((vtype, by_type[vtype]))

    for label, key in (("preview", "preview_file_url"), ("large", "large_file_url"), ("original", "file_url")):
        raw_url = post.get(key)
        if raw_url:
            result.append((label, urljoin(API_BASE + "/", str(raw_url))))
    return result


def normalize_post(post: dict[str, Any], pool_tag: str, pool_label: str) -> dict[str, Any] | None:
    artist = single_artist(post)
    if not artist or not post.get("id"):
        return None
    if post.get("rating") != "g" or post.get("is_deleted") is True:
        return None
    candidates = media_variants(post)
    if not candidates:
        return None
    source = str(post.get("source") or "")
    if not source.startswith(("http://", "https://")):
        source = f"{API_BASE}/posts/{post['id']}"
    return {
        "id": int(post["id"]),
        "artist": artist,
        "poolTag": pool_tag,
        "poolLabel": pool_label,
        "postUrl": f"{API_BASE}/posts/{post['id']}",
        "sourceUrl": source,
        "candidates": candidates,
    }


def fetch_pool(pool_tag: str, pool_label: str) -> list[dict[str, Any]]:
    log(f"Fetching metadata: {pool_label}")
    pages = list(range(1, MAX_PAGE + 1))
    random.shuffle(pages)
    by_id: dict[int, dict[str, Any]] = {}

    for page in pages[:MAX_API_ATTEMPTS]:
        params = urlencode({
            "tags": f"{pool_tag} {RATING_TAG}",
            "limit": FETCH_LIMIT,
            "page": page,
            "only": "id,tag_string_artist,rating,is_deleted,source,file_url,large_file_url,preview_file_url,media_asset",
        })
        url = f"{API_BASE}/posts.json?{params}"
        try:
            payload = retry_json(url, f"{pool_label} page {page}")
        except RuntimeError as exc:
            log(f"  skipping page {page}: {exc}")
            continue
        if not isinstance(payload, list):
            log(f"  page {page}: unexpected response; skipping")
            continue
        usable = 0
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            item = normalize_post(raw, pool_tag, pool_label)
            if item:
                by_id[item["id"]] = item
                usable += 1
        artists = {item["artist"] for item in by_id.values()}
        log(f"  page {page}: {usable} usable; total {len(by_id)} posts / {len(artists)} artists")
        if len(by_id) >= TARGET_PER_POOL * 2 and len(artists) >= max(MIN_UNIQUE_ARTISTS, TARGET_PER_POOL // 2):
            break
        time.sleep(0.35)

    artists = {item["artist"] for item in by_id.values()}
    if len(artists) < MIN_UNIQUE_ARTISTS:
        raise RuntimeError(f"{pool_label}: only {len(artists)} unique artists were found")
    if len(by_id) < TARGET_PER_POOL:
        raise RuntimeError(f"{pool_label}: only {len(by_id)} usable posts were found; need {TARGET_PER_POOL}")
    return list(by_id.values())


def select_varied_posts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shuffled = items[:]
    random.shuffle(shuffled)
    first_by_artist: dict[str, dict[str, Any]] = {}
    for item in shuffled:
        first_by_artist.setdefault(item["artist"], item)

    selected = list(first_by_artist.values())
    random.shuffle(selected)
    selected = selected[:TARGET_PER_POOL]

    if len(selected) < TARGET_PER_POOL:
        used_ids = {item["id"] for item in selected}
        extras = [item for item in shuffled if item["id"] not in used_ids]
        selected.extend(extras[: TARGET_PER_POOL - len(selected)])
    return selected


def download_image(item: dict[str, Any]) -> tuple[str, str]:
    last: Exception | None = None
    for label, url in item["candidates"]:
        host = urlparse(url).hostname or "unknown"
        try:
            data, content_type = request_bytes(
                url,
                accept="image/avif,image/webp,image/*,*/*;q=0.8",
                referer=API_BASE + "/",
                max_bytes=MAX_IMAGE_BYTES,
            )
            if not content_type.startswith("image/"):
                raise ValueError(f"not an image ({content_type})")
            if not data:
                raise ValueError("empty image")
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}", f"{label}@{host}"
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last = exc
            log(f"    post {item['id']} {label}@{host} failed: {exc}")
    raise RuntimeError(f"post {item['id']}: all image candidates failed: {last}")


def build_pool(pool_tag: str, pool_label: str) -> list[dict[str, Any]]:
    metadata = fetch_pool(pool_tag, pool_label)
    candidates = select_varied_posts(metadata)
    result: list[dict[str, Any]] = []
    failures = 0

    # If one selected image fails, keep trying unused metadata until the target is filled.
    selected_ids = {item["id"] for item in candidates}
    fallback = [item for item in metadata if item["id"] not in selected_ids]
    random.shuffle(fallback)
    queue = candidates + fallback

    for item in queue:
        if len(result) >= TARGET_PER_POOL:
            break
        try:
            image_data, route = download_image(item)
        except RuntimeError as exc:
            failures += 1
            log(f"  image skip: {exc}")
            continue
        result.append({
            "id": item["id"],
            "artist": item["artist"],
            "poolTag": item["poolTag"],
            "poolLabel": item["poolLabel"],
            "postUrl": item["postUrl"],
            "sourceUrl": item["sourceUrl"],
            "imageData": image_data,
        })
        log(f"  embedded {len(result):02d}/{TARGET_PER_POOL}: post {item['id']} ({route}, {item['artist']})")
        time.sleep(0.18)

    artists = {item["artist"] for item in result}
    if len(result) < TARGET_PER_POOL:
        raise RuntimeError(f"{pool_label}: embedded only {len(result)}/{TARGET_PER_POOL} artworks ({failures} failures)")
    if len(artists) < MIN_UNIQUE_ARTISTS:
        raise RuntimeError(f"{pool_label}: snapshot has only {len(artists)} unique artists")
    return result


def render_html(game_data: list[dict[str, Any]]) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata = {
        "builtAt": built_at,
        "artworkCount": len(game_data),
        "targetPerPool": TARGET_PER_POOL,
        "pools": [label for _, label in POOLS],
    }
    compact_data = json.dumps(game_data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    compact_meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = template.replace("__GAME_DATA__", compact_data).replace("__BUILD_META__", compact_meta)
    if "__GAME_DATA__" in output or "__BUILD_META__" in output:
        raise RuntimeError("template placeholders were not replaced")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)
    log(f"Built {OUTPUT_PATH} ({len(game_data)} artworks, {size_mb:.2f} MiB)")


def main() -> int:
    random.seed(os.getenv("GITHUB_RUN_ID") or str(time.time_ns()))
    all_items: list[dict[str, Any]] = []
    for pool_tag, pool_label in POOLS:
        all_items.extend(build_pool(pool_tag, pool_label))
    random.shuffle(all_items)
    render_html(all_items)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"BUILD FAILED: {exc}")
        raise
