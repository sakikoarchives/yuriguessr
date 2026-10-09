"""Regression tests: offline, deterministic, and never request Danbooru."""

from __future__ import annotations

import importlib.util
import random
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_game", ROOT / "tools" / "build_game.py")
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", (width, height), (144, 80, 190))
    output = BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


class ImageTests(unittest.TestCase):
    def test_aspect_ratio_is_preserved_for_extreme_images(self):
        for original in [(200, 4000), (4000, 200), (75, 4800), (4800, 75), (1200, 1200), (625, 931)]:
            with self.subTest(original=original):
                data = image_bytes(*original)
                image = builder._prepare_image(data)
                encoded = builder._encode_webp_with_budget(image)
                with Image.open(BytesIO(encoded)) as output:
                    self.assertLessEqual(max(output.size), builder.MAX_IMAGE_DIM)
                    self.assertAlmostEqual(
                        output.width / output.height,
                        original[0] / original[1],
                        delta=max(0.01, 0.015 * original[0] / original[1]),
                    )
                self.assertLessEqual(len(encoded), builder.MAX_EMBED_IMAGE_BYTES)

    def test_crop_candidate_is_rejected_in_favour_of_original(self):
        item = {
            "id": 123,
            "imageWidth": 400,
            "imageHeight": 1200,
            "candidates": [
                ("large", "https://cdn.example/thumbnail.png"),
                ("original", "https://cdn.example/original.png"),
            ],
        }

        def fetch(url, **kwargs):
            if "thumbnail" in url:
                return image_bytes(300, 300), "image/png"
            return image_bytes(400, 1200), "image/png"

        with mock.patch.object(builder, "request_bytes", side_effect=fetch):
            encoded, route = builder.download_image(item)
        self.assertTrue(route.startswith("original@"), route)
        with Image.open(BytesIO(encoded)) as output:
            self.assertAlmostEqual(output.width / output.height, 1 / 3, delta=0.01)

    def test_extreme_panorama_not_rejected_by_integer_pixel_rounding(self):
        item = {
            "id": 789,
            "imageWidth": 4800,
            "imageHeight": 75,
            "candidates": [("large", "https://cdn.example/panorama.png")],
        }
        with mock.patch.object(builder, "request_bytes", return_value=(image_bytes(4800, 75), "image/png")):
            encoded, route = builder.download_image(item)
        with Image.open(BytesIO(encoded)) as picture:
            self.assertEqual(max(picture.size), builder.MAX_IMAGE_DIM)
        self.assertTrue(route.startswith("large@"))

    def test_single_artist_and_general_rating_still_required(self):
        valid = {
            "id": 1,
            "tag_string_artist": "an_artist",
            "rating": "g",
            "is_deleted": False,
            "large_file_url": "https://cdn.example/art.jpg",
        }
        self.assertIsNotNone(builder.normalize_post(valid, "genshin_impact", "Genshin Impact"))
        self.assertIsNone(builder.normalize_post({**valid, "rating": "q"}, "x", "x"))
        self.assertIsNone(builder.normalize_post({**valid, "tag_string_artist": "one two"}, "x", "x"))
        self.assertIsNone(builder.normalize_post({**valid, "is_deleted": True}, "x", "x"))


class BuilderTests(unittest.TestCase):
    def fake_fetch(self, tag: str, label: str):
        # Cross-pool duplicates exist, but every category has enough unique posts.
        offset = {"genshin_impact": 0, "zenless_zone_zero": 100, "honkai_(series)": 200}[tag]
        ids = [1] + list(range(offset + 2, offset + 10))
        return [
            {
                "id": ident,
                "artist": f"{tag}_artist_{index}",
                "poolTag": tag,
                "poolLabel": label,
                "postUrl": f"https://danbooru.donmai.us/posts/{ident}",
                "sourceUrl": f"https://danbooru.donmai.us/posts/{ident}",
                "candidates": [("large", "https://cdn.example/example.png")],
            }
            for index, ident in enumerate(ids)
        ]

    def test_complete_static_site_and_cross_pool_dedup(self):
        with tempfile.TemporaryDirectory() as temp:
            location = Path(temp)
            with (
                mock.patch.object(builder, "TARGET_PER_POOL", 4),
                mock.patch.object(builder, "MIN_PER_POOL", 4),
                mock.patch.object(builder, "DIST_DIR", location / "dist"),
                mock.patch.object(builder, "STAGING_DIR", location / "staging"),
                mock.patch.object(builder, "fetch_pool", side_effect=self.fake_fetch),
                mock.patch.object(builder, "download_image", return_value=(image_bytes(8, 12, "WEBP"), "test")),
                mock.patch.object(builder.time, "sleep"),
            ):
                random.seed(12)
                self.assertEqual(builder.main(), 0)
                html = (location / "dist" / "index.html").read_text()
                images = list((location / "dist" / "images").glob("*.webp"))
                self.assertEqual(len(images), 12)
                self.assertNotIn("__GAME_DATA__", html)
                self.assertNotIn("data:image/webp;base64", html)
                self.assertIn('"imageData":"images/', html)
                self.assertEqual(len({file.stem for file in images}), 12)
                self.assertFalse((location / "staging").exists())

    def test_failure_never_overwrites_last_successful_site(self):
        with tempfile.TemporaryDirectory() as temp:
            location = Path(temp)
            dist = location / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("LAST SUCCESSFUL DEPLOYMENT")

            def fail(tag, label):
                if tag == "zenless_zone_zero":
                    raise RuntimeError("API temporarily unavailable")
                return self.fake_fetch(tag, label)

            with (
                mock.patch.object(builder, "TARGET_PER_POOL", 4),
                mock.patch.object(builder, "MIN_PER_POOL", 4),
                mock.patch.object(builder, "DIST_DIR", dist),
                mock.patch.object(builder, "STAGING_DIR", location / "staging"),
                mock.patch.object(builder, "fetch_pool", side_effect=fail),
                mock.patch.object(builder, "download_image", return_value=(image_bytes(8, 12, "WEBP"), "test")),
                mock.patch.object(builder.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "API temporarily unavailable"):
                    builder.main()
            self.assertEqual((dist / "index.html").read_text(), "LAST SUCCESSFUL DEPLOYMENT")
            self.assertFalse((location / "staging").exists())


if __name__ == "__main__":
    unittest.main()
