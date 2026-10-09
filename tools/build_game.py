#!/usr/bin/env python3
"""Build a static Yuri Guessr snapshot for GitHub Pages.

Only the GitHub Action contacts Danbooru. Artwork is downloaded,
resized without cropping, and saved as individual WebP assets.
The HTML contains a small metadata manifest and loads only needed images.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "index.template.html"
DIST_DIR = ROOT / "dist"
STAGING_DIR = ROOT / "dist.building"

API_BASE = "https://danbooru.donmai.us"
RATING_TAG = "yuri"
POOLS = [
    ("genshin_impact", "Genshin Impact"),
    ("zenless_zone_zero", "Zenless Zone Zero"),
    ("honkai_(series)", "Honkai (series)"),
]

TARGET_PER_POOL = int(os.getenv("TARGET_PER_POOL", "120"))
# A sparse category should still update with the old baseline instead of
# preventing the whole daily deployment when fewer than 120 artworks exist.
MIN_PER_POOL = int(os.getenv("MIN_PER_POOL", "18"))
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "100"))
MAX_PAGE = int(os.getenv("MAX_PAGE", "60"))
MAX_API_ATTEMPTS = int(os.getenv("MAX_API_ATTEMPTS", "45"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
MIN_UNIQUE_ARTISTS = 4

# Never request square thumbnail variants. Validate candidates against Danbooru's
# original image dimensions, then resize proportionally without cropping.
MAX_SOURCE_IMAGE_BYTES = int(os.getenv("MAX_SOURCE_IMAGE_BYTES", "25000000"))
# MAX_EMBED_IMAGE_BYTES remains supported for existing workflow overrides.
MAX_EMBED_IMAGE_BYTES = int(os.getenv("MAX_OUTPUT_IMAGE_BYTES", os.getenv("MAX_EMBED_IMAGE_BYTES", "750000")))
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "1200"))
WEBP_QUALITY = int(os.getenv("WEBP_QUALITY", "84"))
MIN_WEBP_QUALITY = int(os.getenv("MIN_WEBP_QUALITY", "56"))

USER_AGENT = os.getenv(
    "ARTIST_GUESSR_USER_AGENT",
    "ArtistGuessr/1.5 (+https://github.com/; GitHub Actions snapshot builder)",
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


def image_sources(post: dict[str, Any]) -> list[tuple[str, str]]:
    """Return only aspect-ratio-preserving Danbooru image sources.

    Do not use media_asset 720x720 / 360x360 / 180x180 variants or
    preview_file_url here: those may be square crops. The GitHub Action can
    afford to download a larger source and resize it itself without cropping.
    """
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    # large_file_url is usually cheaper to download and preserves aspect ratio.
    # file_url is the original fallback if a large version is unavailable.
    for label, key in (("large", "large_file_url"), ("original", "file_url")):
        raw_url = post.get(key)
        if not raw_url:
            continue
        url = urljoin(API_BASE + "/", str(raw_url))
        if url in seen:
            continue
        seen.add(url)
        result.append((label, url))

    return result


def normalize_post(post: dict[str, Any], pool_tag: str, pool_label: str) -> dict[str, Any] | None:
    artist = single_artist(post)
    if not artist or not post.get("id"):
        return None
    if post.get("rating") != "g" or post.get("is_deleted") is True:
        return None
    candidates = image_sources(post)
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
        "imageWidth": int(post.get("image_width") or 0),
        "imageHeight": int(post.get("image_height") or 0),
    }


def fetch_pool(pool_tag: str, pool_label: str) -> list[dict[str, Any]]:
    log(f"Fetching metadata: {pool_label}")
    # Always inspect the newest page; random older pages vary the daily snapshot.
    pages = list(range(2, MAX_PAGE + 1))
    random.shuffle(pages)
    pages.insert(0, 1)
    by_id: dict[int, dict[str, Any]] = {}

    for page in pages[:MAX_API_ATTEMPTS]:
        params = urlencode({
            "tags": f"{pool_tag} {RATING_TAG}",
            "limit": FETCH_LIMIT,
            "page": page,
            "only": "id,tag_string_artist,rating,is_deleted,source,file_url,large_file_url,image_width,image_height",
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
        time.sleep(0.8)

    artists = {item["artist"] for item in by_id.values()}
    if len(artists) < MIN_UNIQUE_ARTISTS:
        raise RuntimeError(f"{pool_label}: only {len(artists)} unique artists were found")
    if len(by_id) < MIN_PER_POOL:
        raise RuntimeError(f"{pool_label}: only {len(by_id)} usable posts were found; need at least {MIN_PER_POOL}")
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


def _prepare_image(data: bytes) -> Image.Image:
    """Decode one image frame and normalize orientation without cropping."""
    with Image.open(BytesIO(data)) as source:
        # Animated files are intentionally flattened to their first frame; the
        # frame keeps the original canvas/aspect ratio.
        try:
            source.seek(0)
        except EOFError:
            pass
        image = ImageOps.exif_transpose(source).copy()

    # Keep transparency where it exists; otherwise use RGB for smaller WebP.
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    return image.convert("RGBA" if has_alpha else "RGB")


def _resize_to_long_edge(image: Image.Image, max_dim: int) -> Image.Image:
    """Scale down to max_dim while preserving aspect ratio; never crop/upscale."""
    resized = image.copy()
    if resized.width > max_dim or resized.height > max_dim:
        resized.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return resized


def _encode_webp_with_budget(image: Image.Image) -> bytes:
    """Encode as WebP under the target budget without changing aspect ratio."""
    working = _resize_to_long_edge(image, MAX_IMAGE_DIM)

    # First lower quality. If that is still too large, reduce both dimensions
    # proportionally and try again. At no point do we crop or force a square.
    while True:
        for quality in range(WEBP_QUALITY, MIN_WEBP_QUALITY - 1, -7):
            output = BytesIO()
            working.save(output, format="WEBP", quality=quality, method=6)
            payload = output.getvalue()
            if len(payload) <= MAX_EMBED_IMAGE_BYTES:
                return payload

        longest = max(working.size)
        if longest <= 96:
            # Preserve the byte budget even for complex or noisy images.
            raise ValueError(f"WebP exceeds {MAX_EMBED_IMAGE_BYTES} bytes at minimum dimension")

        next_longest = max(96, int(longest * 0.85))
        new_width = max(1, round(working.width * next_longest / longest))
        new_height = max(1, round(working.height * next_longest / longest))
        working = working.resize((new_width, new_height), Image.Resampling.LANCZOS)


def download_image(item: dict[str, Any]) -> tuple[bytes, str]:
    last: Exception | None = None
    for label, url in item["candidates"]:
        host = urlparse(url).hostname or "unknown"
        try:
            data, content_type = request_bytes(
                url,
                accept="image/avif,image/webp,image/*,*/*;q=0.8",
                referer=API_BASE + "/",
                max_bytes=MAX_SOURCE_IMAGE_BYTES,
            )
            if not content_type.startswith("image/"):
                raise ValueError(f"not an image ({content_type})")
            if not data:
                raise ValueError("empty image")

            image = _prepare_image(data)
            original_size = image.size
            expected_width = item.get("imageWidth") or 0
            expected_height = item.get("imageHeight") or 0
            if expected_width > 0 and expected_height > 0:
                # A source URL can unexpectedly point at a cropped rendition.
                # Refuse it and try the original file instead.
                expected_ratio = expected_width / expected_height
                actual_ratio = image.width / image.height
                if abs(expected_ratio - actual_ratio) > 0.015 * expected_ratio:
                    raise ValueError(
                        f"source does not match original aspect ratio: "
                        f"{original_size} vs {expected_width}x{expected_height}"
                    )
            encoded_bytes = _encode_webp_with_budget(image)

            # Re-open the result only for logging/verification of final geometry.
            with Image.open(BytesIO(encoded_bytes)) as encoded_image:
                final_size = encoded_image.size

            # Compare the shorter edge after proportional scaling. Allow a
            # single rounding pixel: absolute ratio comparisons incorrectly
            # reject very wide panoramas after downsizing to integer pixels.
            expected_short = min(original_size) * max(final_size) / max(original_size)
            if abs(min(final_size) - expected_short) > 1.1:
                raise ValueError(
                    f"aspect ratio changed unexpectedly: {original_size} -> {final_size}"
                )

            route = (
                f"{label}@{host} {original_size[0]}x{original_size[1]}"
                f"->{final_size[0]}x{final_size[1]} {len(encoded_bytes) // 1024}KiB"
            )
            return encoded_bytes, route
        except (HTTPError, URLError, TimeoutError, ValueError, OSError, UnidentifiedImageError) as exc:
            last = exc
            log(f"    post {item['id']} {label}@{host} failed: {exc}")
    raise RuntimeError(f"post {item['id']}: all aspect-safe image sources failed: {last}")


def build_pool(
    pool_tag: str, pool_label: str, image_dir: Path, excluded_ids: set[int],
) -> list[dict[str, Any]]:
    # A single post can have tags from several games. Never publish it twice.
    metadata = [item for item in fetch_pool(pool_tag, pool_label) if item["id"] not in excluded_ids]
    if len(metadata) < MIN_PER_POOL:
        raise RuntimeError(f"{pool_label}: only {len(metadata)} distinct posts remain after cross-pool deduplication")
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
            image_bytes, route = download_image(item)
        except RuntimeError as exc:
            failures += 1
            log(f"  image skip: {exc}")
            continue
        image_dir.joinpath(f"{item['id']}.webp").write_bytes(image_bytes)
        result.append({
            "id": item["id"],
            "artist": item["artist"],
            "poolTag": item["poolTag"],
            "poolLabel": item["poolLabel"],
            "postUrl": item["postUrl"],
            "sourceUrl": item["sourceUrl"],
            "imageData": f"images/{item['id']}.webp",
        })
        log(f"  saved {len(result):03d}/{TARGET_PER_POOL}: post {item['id']} ({route}, {item['artist']})")
        time.sleep(0.9)

    artists = {item["artist"] for item in result}
    if len(result) < MIN_PER_POOL:
        raise RuntimeError(f"{pool_label}: saved only {len(result)}/{TARGET_PER_POOL} artworks ({failures} failures)")
    if len(artists) < MIN_UNIQUE_ARTISTS:
        raise RuntimeError(f"{pool_label}: snapshot has only {len(artists)} unique artists")
    return result


def render_html(game_data: list[dict[str, Any]], output_dir: Path) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata = {
        "builtAt": built_at,
        "artworkCount": len(game_data),
        "targetPerPool": TARGET_PER_POOL,
    "minPerPool": MIN_PER_POOL,
        "pools": [label for _, label in POOLS],
    }
    compact_data = json.dumps(game_data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    compact_meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = template.replace("__GAME_DATA__", compact_data).replace("__BUILD_META__", compact_meta)
    if "__GAME_DATA__" in output or "__BUILD_META__" in output:
        raise RuntimeError("template placeholders were not replaced")
    output_path = output_dir / "index.html"
    output_path.write_text(output, encoding="utf-8")
    size_mb = output_path.stat().st_size / (1024 * 1024)
    log(f"Built {output_path} ({len(game_data)} artworks, HTML {size_mb:.2f} MiB)")


def main() -> int:
    random.seed(os.getenv("GITHUB_RUN_ID") or str(time.time_ns()))
    # Only replace the published snapshot after a complete successful build.
    # A transient Danbooru error must not leave a half-built local dist tree.
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    image_dir = STAGING_DIR / "images"
    image_dir.mkdir(parents=True)
    all_items: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    try:
        for pool_tag, pool_label in POOLS:
            items = build_pool(pool_tag, pool_label, image_dir, used_ids)
            used_ids.update(item["id"] for item in items)
            all_items.extend(items)
        random.shuffle(all_items)
        render_html(all_items, STAGING_DIR)
        snapshot_bytes = sum(file.stat().st_size for file in image_dir.iterdir())
        log(f"Snapshot: {len(all_items)} unique posts; images {snapshot_bytes / 1024**2:.1f} MiB")
        if DIST_DIR.exists():
            shutil.rmtree(DIST_DIR)
        STAGING_DIR.rename(DIST_DIR)
        return 0
    finally:
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"BUILD FAILED: {exc}")
        raise
