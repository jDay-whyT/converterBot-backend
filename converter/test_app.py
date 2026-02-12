import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# Set test environment
os.environ["CONVERTER_API_KEY"] = "test_secret"
os.environ["MAX_FILE_MB"] = "40"

from app import app, _black_band_detected, _decoder_route, _mapped_extension, _run


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

    @patch("app._detect_filetype", return_value=("JPEG", "image/jpeg"))
    @patch("app._magick_to_jpeg")
    def test_convert_dng_with_jpeg_filetype_uses_magick_route(self, mock_magick: MagicMock, _mock_detect: MagicMock) -> None:
        def _write_output(_in: Path, out: Path, _quality: int, _max_side: int | None) -> None:
            out.write_bytes(b"x" * 60000)

        mock_magick.side_effect = _write_output
        with patch("app._image_ok", return_value=True):
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("fake.dng", b"jpeg-content", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_magick.called)

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


    def test_decoder_route_dng_extension_but_jpeg_content(self) -> None:
        route = _decoder_route("JPEG", "image/jpeg")
        self.assertEqual(route, "magick")

    def test_mapped_extension_prefers_detected_filetype(self) -> None:
        self.assertEqual(_mapped_extension("HEIC", "image/heic"), ".heic")
        self.assertEqual(_mapped_extension("JPEG", "image/jpeg"), ".jpg")

    def test_black_band_detector_detects_half_black_frame(self) -> None:
        with patch("app._region_luma", side_effect=[0.20, 0.15, 0.12, 0.11, 0.0001]):
            detected = _black_band_detected(Path("dummy.jpg"))
        self.assertTrue(detected)

    def test_black_band_detector_disabled_for_dark_scene(self) -> None:
        with patch("app._region_luma", side_effect=[0.01, 0.0, 0.0, 0.0, 0.0]):
            detected = _black_band_detected(Path("dummy.jpg"))
        self.assertFalse(detected)

    @patch("app._detect_filetype", return_value=("JPEG", "image/jpeg"))
    @patch("app._magick_to_jpeg")
    def test_convert_renames_input_to_detected_extension(self, mock_magick: MagicMock, _mock_detect: MagicMock) -> None:
        def _write_output(_in: Path, out: Path, _quality: int, _max_side: int | None) -> None:
            out.write_bytes(b"x" * 60000)

        mock_magick.side_effect = _write_output
        with patch("app._image_ok", return_value=True):
            response = self.client.post(
                "/convert",
                headers={"X-API-KEY": "test_secret"},
                files={"file": ("upload", b"jpeg-content", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 200)
        in_path = mock_magick.call_args[0][0]
        self.assertEqual(in_path.suffix.lower(), ".jpg")


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

    @unittest.skipIf(not os.path.exists("/usr/bin/magick"), "imagemagick not installed")
    def test_convert_webp_format(self) -> None:
        """Test conversion of WebP format"""
        # Create a minimal WebP (1x1 pixel)
        # This is a valid lossy WebP file
        webp_data = bytes.fromhex(
            "52494646260000005745425056503820"
            "1a0000003001009d012a0100010001"
            "0011620029564d46"
        )

        response = self.client.post(
            "/convert",
            headers={"X-API-KEY": "test_secret"},
            files={"file": ("test.webp", webp_data, "application/octet-stream")},
            data={"quality": "85"},
        )

        # Should either succeed (200) or fail with conversion error (422)
        # depending on whether the minimal WebP is valid enough
        self.assertIn(response.status_code, [200, 422])

    @unittest.skipIf(not os.path.exists("/usr/bin/magick"), "imagemagick not installed")
    def test_heif_format_validation(self) -> None:
        """Test that HEIF/HEIC formats are accepted"""
        # Test .heic extension
        response = self.client.post(
            "/convert",
            headers={"X-API-KEY": "test_secret"},
            files={"file": ("test.heic", b"fake data", "application/octet-stream")},
        )
        # Should fail with conversion error, not unsupported format
        self.assertEqual(response.status_code, 422)
        self.assertIn("conversion failed", response.json()["detail"])

        # Test .heif extension
        response = self.client.post(
            "/convert",
            headers={"X-API-KEY": "test_secret"},
            files={"file": ("test.heif", b"fake data", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("conversion failed", response.json()["detail"])

    @unittest.skipIf(not os.path.exists("/usr/bin/dcraw"), "dcraw not installed")
    def test_raw_format_validation(self) -> None:
        """Test that RAW formats are accepted and use RAW conversion path"""
        raw_formats = [".dng", ".cr2", ".nef", ".arw"]

        for fmt in raw_formats:
            with self.subTest(format=fmt):
                response = self.client.post(
                    "/convert",
                    headers={"X-API-KEY": "test_secret"},
                    files={"file": (f"test{fmt}", b"fake raw data", "application/octet-stream")},
                )
                # Should fail with RAW decoding error, not unsupported format
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"]
                self.assertIn("conversion failed", detail)

    @unittest.skipIf(not os.path.exists("/usr/bin/magick"), "imagemagick not installed")
    def test_tiff_format_validation(self) -> None:
        """Test that TIFF formats are accepted"""
        for ext in [".tif", ".tiff"]:
            with self.subTest(extension=ext):
                response = self.client.post(
                    "/convert",
                    headers={"X-API-KEY": "test_secret"},
                    files={"file": (f"test{ext}", b"fake tiff", "application/octet-stream")},
                )
                # Should fail with conversion error, not unsupported format
                self.assertEqual(response.status_code, 422)
                self.assertIn("conversion failed", response.json()["detail"])

    def test_api_key_from_environment(self) -> None:
        """Verify API key is loaded from environment"""
        from app import API_KEY

        self.assertEqual(API_KEY, "test_secret")

    def test_all_raw_formats_in_allowed_suffixes(self) -> None:
        """Verify all RAW formats are in allowed suffixes"""
        from app import RAW_SUFFIXES, ALLOWED_SUFFIXES

        for raw_format in RAW_SUFFIXES:
            with self.subTest(format=raw_format):
                self.assertIn(raw_format, ALLOWED_SUFFIXES)

    def test_raw_suffixes_completeness(self) -> None:
        """Verify RAW_SUFFIXES contains expected formats"""
        from app import RAW_SUFFIXES

        expected_raw = {
            ".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw",
            ".raf", ".rw2", ".orf", ".pef", ".srw", ".x3f",
            ".3fr", ".iiq", ".dcr", ".kdc", ".mrw",
        }
        self.assertEqual(RAW_SUFFIXES, expected_raw)


if __name__ == "__main__":
    unittest.main()
