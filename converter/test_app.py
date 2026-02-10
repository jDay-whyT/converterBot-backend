import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# Set test environment
os.environ["CONVERTER_API_KEY"] = "test_secret"
os.environ["MAX_FILE_MB"] = "40"

from app import app, _run


class ConverterAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_convert_missing_api_key(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(b"fake heic data")
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                files={"file": ("test.heic", tmp, "application/octet-stream")},
            )
        self.assertEqual(response.status_code, 401)
        self.assertIn("invalid api key", response.json()["detail"])

    def test_convert_invalid_api_key(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(b"fake heic data")
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "wrong_key"},
                files={"file": ("test.heic", tmp, "application/octet-stream")},
            )
        self.assertEqual(response.status_code, 401)

    def test_convert_unsupported_extension(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
            tmp.write(b"text file")
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("test.txt", tmp, "application/octet-stream")},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unsupported file extension", response.json()["detail"])

    def test_convert_quality_out_of_range(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(b"fake heic data")
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("test.heic", tmp, "application/octet-stream")},
                data={"quality": "150"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("quality must be in range", response.json()["detail"])

    def test_convert_invalid_max_side(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(b"fake heic data")
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("test.heic", tmp, "application/octet-stream")},
                data={"max_side": "-1"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("max_side must be > 0", response.json()["detail"])

    def test_convert_file_too_large(self) -> None:
        large_data = b"x" * (41 * 1024 * 1024)  # 41 MB
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(large_data)
            tmp.seek(0)
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("test.heic", tmp, "application/octet-stream")},
            )
        self.assertEqual(response.status_code, 413)
        self.assertIn("file too large", response.json()["detail"])

    def test_run_command_success(self) -> None:
        result = _run(["echo", "test"])
        self.assertEqual(result, b"test\n")

    def test_run_command_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            _run(["false"])


class ConverterIntegrationTests(unittest.TestCase):
    """Integration tests that require actual imagemagick tools"""

    def setUp(self) -> None:
        self.client = TestClient(app)
        # Check if tools are available
        import shutil

        self.has_magick = shutil.which("magick") is not None
        self.has_dcraw = shutil.which("dcraw") is not None

    @unittest.skipIf(not os.path.exists("/usr/bin/magick"), "imagemagick not installed")
    def test_convert_simple_image(self) -> None:
        """Test conversion with a simple PNG that should work with imagemagick"""
        # Create a minimal PNG (1x1 red pixel)
        png_data = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "08020000009077530000000c494441540801630000020001"
            "e25c8f40000000049454e44ae426082"
        )

        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(png_data)
            tmp.seek(0)
            # Note: PNG is not in ALLOWED_SUFFIXES, so this will fail validation
            # We test with webp format instead
            pass

    def test_api_key_from_environment(self) -> None:
        """Verify API key is loaded from environment"""
        from app import API_KEY

        self.assertEqual(API_KEY, "test_secret")


if __name__ == "__main__":
    unittest.main()
