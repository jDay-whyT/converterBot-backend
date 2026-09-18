import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

API_KEY = os.getenv("CONVERTER_API_KEY", "")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "40"))

# Pub/Sub job processing (merged in from the former standalone worker service,
# to avoid shipping the RAW file bytes over the network twice: Telegram->worker->converter).
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")
TOPIC_CONVERTED_ID = int(os.getenv("TOPIC_CONVERTED_ID", "0") or "0")
CONVERSION_QUALITY = int(os.getenv("CONVERSION_QUALITY", "92"))

RAW_SUFFIXES = {
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".nrw",
    ".arw",
    ".raf",
    ".rw2",
    ".orf",
    ".pef",
    ".srw",
    ".x3f",
    ".3fr",
    ".iiq",
    ".dcr",
    ".kdc",
    ".mrw",
}

RAW_MIME_PREFIXES = (
    "image/x-",
    "image/raw",
    "image/dng",
    "image/prs.adobe.dng",
)

RAW_MIME_TYPES = {
    "image/x-adobe-dng",
    "image/x-canon-cr2",
    "image/x-canon-cr3",
    "image/x-nikon-nef",
    "image/x-nikon-nrw",
    "image/x-sony-arw",
    "image/x-fuji-raf",
    "image/x-panasonic-rw2",
    "image/x-olympus-orf",
    "image/x-pentax-pef",
    "image/x-samsung-srw",
    "image/x-sigma-x3f",
    "image/x-hasselblad-3fr",
    "image/x-phaseone-iiq",
    "image/x-kodak-dcr",
    "image/x-kodak-kdc",
    "image/x-minolta-mrw",
}

ALLOWED_SUFFIXES = {".heic", ".heif", ".webp", ".tif", ".tiff", *RAW_SUFFIXES}
RAW_DECODE_SUFFIXES = (".tiff", ".tif", ".ppm", ".pgm")

FILETYPE_EXTENSION_MAP = {
    "heic": ".heic",
    "heif": ".heif",
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "tif": ".tif",
    "tiff": ".tiff",
    "webp": ".webp",
    **{suffix[1:]: suffix for suffix in RAW_SUFFIXES},
}

SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv("SUBPROCESS_TIMEOUT_SECONDS", "90"))
# Extracting an embedded preview either returns in a couple seconds or the tag is
# absent entirely; up to 3 tags are tried in sequence, so keep this well under
# SUBPROCESS_TIMEOUT_SECONDS to leave headroom under the shared 600s request budget.
EXIFTOOL_PREVIEW_TIMEOUT_SECONDS = int(os.getenv("EXIFTOOL_PREVIEW_TIMEOUT_SECONDS", "20"))
MAGICK_TIMEOUT_SECONDS = int(os.getenv("MAGICK_TIMEOUT_SECONDS", "90"))
DCRAW_TIMEOUT_SECONDS = int(os.getenv("DCRAW_TIMEOUT_SECONDS", "120"))
DARKTABLE_TIMEOUT_SECONDS = int(os.getenv("DARKTABLE_TIMEOUT_SECONDS", "180"))

DEFAULT_SUBPROCESS_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}

MAX_STDERR_CHARS = int(os.getenv("MAX_STDERR_CHARS", "4096"))
MIN_OUTPUT_BYTES = int(os.getenv("MIN_OUTPUT_BYTES", str(50 * 1024)))
MIN_INPUT_BYTES = int(os.getenv("MIN_INPUT_BYTES", str(100 * 1024)))
MIN_BLACK_BAND_LUMA = float(os.getenv("MIN_BLACK_BAND_LUMA", "0.002"))
MIN_SCENE_LUMA_FOR_BAND_CHECK = float(os.getenv("MIN_SCENE_LUMA_FOR_BAND_CHECK", "0.03"))

_bot: Bot | None = None
_processed_jobs: dict[str, None] = {}  # insertion-ordered for correct FIFO eviction


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot
    logging.basicConfig(level=logging.INFO)
    await _check_tools()

    if BOT_TOKEN:
        _bot = Bot(token=BOT_TOKEN)

    missing_pubsub_config = [
        name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID), ("TOPIC_CONVERTED_ID", TOPIC_CONVERTED_ID))
        if not value
    ]
    if missing_pubsub_config:
        # Not fatal: /convert must keep working even without these. But log loudly
        # (not just on first request) so a dropped env var on a manual `gcloud run
        # services update` shows up at deploy time, not as silent 503s later.
        logging.error(
            "pubsub_config_incomplete missing=%s -- /pubsub/push will reject all jobs with 503 until fixed",
            ",".join(missing_pubsub_config),
        )

    yield

    if _bot:
        await _bot.session.close()


app = FastAPI(title="converter-service", lifespan=lifespan)


def _is_file_too_big_error(exc: TelegramBadRequest) -> bool:
    return "file is too big" in str(exc).lower()


def _safe_filename(name: Optional[str], fallback: str) -> str:
    """Collapse to a bare filename, discarding any directory/traversal segments.

    file_name comes from the Pub/Sub job payload (attacker-reachable via the
    unauthenticated /pubsub/push endpoint) and is used to build a filesystem
    path, so it must never be joined in raw.
    """
    candidate = Path(name).name if name else ""
    if not candidate or candidate in (".", ".."):
        return fallback
    return candidate


async def _tg_retry(fn, *args, max_retries: int = 3, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as exc:
            if attempt == max_retries:
                raise
            sleep_time = exc.retry_after + 1
            logging.warning(
                "TelegramRetryAfter fn=%s attempt=%s/%s sleeping=%ss",
                fn.__name__, attempt + 1, max_retries, sleep_time,
            )
            await asyncio.sleep(sleep_time)
        except TelegramNetworkError as exc:
            if attempt == max_retries:
                raise
            sleep_time = 2 ** attempt
            logging.warning(
                "TelegramNetworkError fn=%s attempt=%s/%s sleeping=%ss error=%s",
                fn.__name__, attempt + 1, max_retries, sleep_time, exc,
            )
            await asyncio.sleep(sleep_time)


def format_ms(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return int(seconds * 1000)


@dataclass
class CommandError:
    tool: str
    returncode: Optional[int]
    stderr: str
    timeout: bool


class CommandExecutionError(RuntimeError):
    def __init__(self, tool: str, returncode: Optional[int], stderr: str, timeout: bool = False):
        self.tool = tool
        self.returncode = returncode
        self.stderr = stderr
        self.timeout = timeout
        kind = "timeout" if timeout else "failed"
        super().__init__(f"{tool} {kind}: {stderr}")


def _truncate_stderr(stderr: str, limit: int = MAX_STDERR_CHARS) -> str:
    cleaned = stderr.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}...[truncated {len(cleaned) - limit} chars]"


def _run(
    cmd: list[str],
    input_bytes: bytes | None = None,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
    env_overrides: Optional[dict[str, str]] = None,
    return_stderr: bool = False,
) -> bytes | tuple[bytes, str]:
    tool = cmd[0]
    env = os.environ.copy()
    env.update(DEFAULT_SUBPROCESS_ENV)
    if env_overrides:
        env.update(env_overrides)

    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CommandExecutionError(tool=tool, returncode=None, stderr="command not found") from exc
    except subprocess.TimeoutExpired as exc:
        stderr = _truncate_stderr((exc.stderr or b"").decode("utf-8", errors="ignore") or f"timeout after {timeout}s")
        raise CommandExecutionError(tool=tool, returncode=None, stderr=stderr, timeout=True) from exc

    stderr = _truncate_stderr(proc.stderr.decode("utf-8", errors="ignore") or "")

    if proc.returncode != 0:
        raise CommandExecutionError(
            tool=tool,
            returncode=proc.returncode,
            stderr=stderr or f"command failed: {' '.join(cmd)}",
        )
    if return_stderr:
        return proc.stdout, stderr
    return proc.stdout


def _magick_to_jpeg(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    cmd = [
        "magick",
        "-limit",
        "thread",
        "1",
        str(input_path),
        "-auto-orient",
        "-colorspace",
        "sRGB",
    ]
    if max_side:
        cmd.extend(["-resize", f"{max_side}x{max_side}>"])
    cmd.extend(["-quality", str(quality), "-strip", str(output_path)])
    _run(cmd, timeout=MAGICK_TIMEOUT_SECONDS)


def _validate_output_file(path: Path, min_size_bytes: int = MIN_OUTPUT_BYTES) -> None:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"output file missing: {path}")
    size = path.stat().st_size
    if size < min_size_bytes:
        raise RuntimeError(f"output file too small: {path} ({size} bytes)")


def _identify_dimensions(path: Path) -> Optional[tuple[int, int]]:
    try:
        result = _run(["magick", "identify", "-format", "%w %h", str(path)], timeout=MAGICK_TIMEOUT_SECONDS)
        width_str, height_str = result.decode("utf-8", errors="ignore").strip().split(maxsplit=1)
        width = int(width_str)
        height = int(height_str)
    except Exception:
        return None
    return width, height


def _identify_ok(path: Path, min_dimension: int = 200) -> bool:
    dims = _identify_dimensions(path)
    if not dims:
        return False
    width, height = dims
    return width >= min_dimension and height >= min_dimension


def _region_luma(path: Path, crop: str, gravity: Optional[str] = None) -> float:
    try:
        cmd = [
            "magick",
            str(path),
            "-colorspace",
            "Gray",
        ]
        if gravity:
            cmd.extend(["-gravity", gravity])
        cmd.extend([
            "-crop",
            crop,
            "-resize",
            "64x64!",
            "-format",
            "%[fx:mean]",
            "info:",
        ])
        result = _run(
            cmd,
            timeout=MAGICK_TIMEOUT_SECONDS,
        )
        return float(result.decode("utf-8", errors="ignore").strip())
    except Exception:
        return -1.0


def _black_band_detected(path: Path) -> bool:
    full = _region_luma(path, "100%x100%")
    left = _region_luma(path, "50%x100%+0+0", gravity="West")
    right = _region_luma(path, "50%x100%+0+0", gravity="East")
    top = _region_luma(path, "100%x50%+0+0", gravity="North")
    bottom = _region_luma(path, "100%x50%+0+0", gravity="South")

    mean_edges = (left, right, top, bottom)
    black_band = full >= MIN_SCENE_LUMA_FOR_BAND_CHECK and min(mean_edges) < MIN_BLACK_BAND_LUMA
    mean_str = lambda val: f"{val:.6f}" if val >= 0 else "na"
    logging.info(
        "img_check mean_full=%s mean_l=%s mean_r=%s mean_t=%s mean_b=%s black_band=%d",
        mean_str(full), mean_str(left), mean_str(right), mean_str(top), mean_str(bottom), int(black_band),
    )
    return black_band


def _image_fail_reason(path: Path, min_dimension: int = 200) -> Optional[str]:
    if not _identify_ok(path, min_dimension=min_dimension):
        return "identify_failed"

    if _black_band_detected(path):
        return "black_band_detected"

    return None


def _image_ok(path: Path, min_dimension: int = 200) -> bool:
    return _image_fail_reason(path, min_dimension=min_dimension) is None


def _find_decoded_raw_path(input_path: Path) -> Path:
    candidates: list[Path] = []
    for suffix in RAW_DECODE_SUFFIXES:
        direct = input_path.with_suffix(suffix)
        if direct.exists() and direct.is_file():
            candidates.append(direct)
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    pattern = f"{input_path.stem}*"
    for candidate in input_path.parent.glob(pattern):
        if candidate.suffix.lower() in RAW_DECODE_SUFFIXES and candidate.is_file():
            candidates.append(candidate)

    if not candidates:
        raise RuntimeError(f"decoded RAW output not found for {input_path.name}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _format_raw_errors(errors: list[CommandError]) -> str:
    parts = []
    for err in errors:
        timeout = " timeout=1" if err.timeout else ""
        returncode = "na" if err.returncode is None else str(err.returncode)
        parts.append(f"raw_step={err.tool} status=fail reason={err.stderr} rc={returncode}{timeout}")
    return " | ".join(parts)


def _detect_filetype(input_path: Path) -> tuple[str, str]:
    output = _run(["exiftool", "-s3", "-FileType", "-MIMEType", str(input_path)])
    values = [line.strip() for line in output.decode("utf-8", errors="ignore").splitlines() if line.strip()]
    if len(values) < 2:
        raise RuntimeError("failed to detect file type")
    return values[0], values[1].lower()


def _decoder_route(file_type: str, mime_type: str) -> Literal["heif", "magick", "raw"]:
    normalized_type = file_type.lower()
    if normalized_type in {"heic", "heif"} or mime_type in {"image/heic", "image/heif"}:
        return "heif"
    if normalized_type in {"jpeg", "jpg", "png", "tif", "tiff", "webp"} or mime_type in {
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    }:
        return "magick"
    if normalized_type in {suffix[1:] for suffix in RAW_SUFFIXES}:
        return "raw"
    if mime_type in RAW_MIME_TYPES or any(mime_type.startswith(prefix) for prefix in RAW_MIME_PREFIXES):
        return "raw"
    raise RuntimeError(f"unsupported detected file type: {file_type} ({mime_type})")


def _mapped_extension(file_type: str, mime_type: str) -> Optional[str]:
    normalized_type = file_type.lower()
    mapped = FILETYPE_EXTENSION_MAP.get(normalized_type)
    if mapped:
        return mapped

    mime_map = {
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/tiff": ".tiff",
        "image/webp": ".webp",
    }
    return mime_map.get(mime_type)


def _convert_raw(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    errors: list[CommandError] = []
    # dcraw parameters: -T (TIFF output), -w (camera white balance), -q 3 (cubic interpolation),
    # -H 2 (highlight recovery mode 2 - reconstructs clipped highlights), -6 (16-bit output for better dynamic range)
    decode_cmd = ["-T", "-w", "-q", "3", "-H", "2", "-6", str(input_path)]

    def _record_fail(tool: str, reason: str, returncode: Optional[int] = None, timeout: bool = False) -> None:
        stderr = _truncate_stderr(reason)
        rc = "na" if returncode is None else str(returncode)
        logging.warning("raw_step=%s status=fail reason=%s timeout=%d rc=%s", tool, stderr, int(timeout), rc)
        errors.append(CommandError(tool=tool, returncode=returncode, stderr=stderr, timeout=timeout))

    # A) exiftool embedded preview -> magick -> jpg
    if shutil.which("exiftool") is None:
        _record_fail("exiftool", "command not found")
    else:
        preview_path = input_path.with_name("raw_preview.jpg")
        for preview_tag in ("PreviewImage", "JpgFromRaw", "ThumbnailImage"):
            try:
                with open(preview_path, "wb") as preview_file:
                    proc = subprocess.run(
                        ["exiftool", "-b", f"-{preview_tag}", str(input_path)],
                        stdout=preview_file,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=EXIFTOOL_PREVIEW_TIMEOUT_SECONDS,
                        env={**os.environ, **DEFAULT_SUBPROCESS_ENV},
                    )
                stderr = _truncate_stderr(proc.stderr.decode("utf-8", errors="ignore") or "")
                if proc.returncode != 0:
                    if "doesn't exist" in stderr.lower() or "not found" in stderr.lower():
                        logging.info("raw_step=exiftool:%s status=skip reason=tag_missing_or_empty rc=%s", preview_tag, proc.returncode)
                        preview_path.unlink(missing_ok=True)
                        continue
                    raise CommandExecutionError("exiftool", proc.returncode, stderr or "preview extraction failed")
                try:
                    _validate_output_file(preview_path)
                except RuntimeError:
                    logging.info("raw_step=exiftool:%s status=skip reason=tag_missing_or_empty rc=0", preview_tag)
                    preview_path.unlink(missing_ok=True)
                    continue
                fail_reason = _image_fail_reason(preview_path)
                if fail_reason:
                    _record_fail(f"exiftool:{preview_tag}", fail_reason)
                    preview_path.unlink(missing_ok=True)
                    continue
                _magick_to_jpeg(preview_path, output_path, quality, max_side)
                _validate_output_file(output_path)
                fail_reason = _image_fail_reason(output_path)
                if fail_reason:
                    _record_fail(f"exiftool:{preview_tag}", fail_reason)
                    continue
                logging.info("raw_step=exiftool:%s status=ok reason=preview_extracted", preview_tag)
                return
            except CommandExecutionError as exc:
                _record_fail(f"exiftool:{preview_tag}", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
                preview_path.unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                _record_fail(f"exiftool:{preview_tag}", f"timeout after {EXIFTOOL_PREVIEW_TIMEOUT_SECONDS}s", timeout=True)
                preview_path.unlink(missing_ok=True)
            except Exception as exc:
                _record_fail(f"exiftool:{preview_tag}", str(exc))
                preview_path.unlink(missing_ok=True)

    # B) darktable-cli -> jpg -> magick -> jpg
    if shutil.which("darktable-cli") is None:
        _record_fail("darktable-cli", "command not found")
    else:
        try:
            darktable_jpg = input_path.with_name("raw_darktable.jpg")
            _run([
                "darktable-cli",
                str(input_path),
                str(darktable_jpg),
                "--core",
                "--conf",
                "plugins/imageio/format/jpeg/quality=95",
                "--conf",
                "plugins/imageio/format/jpeg/allow_upscale=false",
                "--conf",
                "opencl=false",
            ], timeout=DARKTABLE_TIMEOUT_SECONDS, env_overrides={"DARKTABLE_NUM_THREADS": "1"})
            _validate_output_file(darktable_jpg)
            if _black_band_detected(darktable_jpg):
                raise RuntimeError("black_band_detected")
            _magick_to_jpeg(darktable_jpg, output_path, quality, max_side)
            _validate_output_file(output_path)
            fail_reason = _image_fail_reason(output_path)
            if fail_reason:
                raise RuntimeError(fail_reason)
            logging.info("raw_step=darktable-cli status=ok reason=render_success")
            return
        except CommandExecutionError as exc:
            _record_fail("darktable-cli", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("darktable-cli", str(exc))
        except Exception as exc:
            _record_fail("darktable-cli", str(exc))

    # C1) rawtherapee-cli -> TIFF, then magick -> JPG
    if shutil.which("rawtherapee-cli") is None:
        _record_fail("rawtherapee-cli", "command not found")
    else:
        try:
            rawtherapee_tif = input_path.with_name("rawtherapee.tif")
            _run(
                [
                    "rawtherapee-cli",
                    "-Y",
                    "-c",
                    str(input_path),
                    "-o",
                    str(rawtherapee_tif),
                    "-t",
                ],
                timeout=DCRAW_TIMEOUT_SECONDS,
            )
            _validate_output_file(rawtherapee_tif)
            fail_reason = _image_fail_reason(rawtherapee_tif)
            if fail_reason:
                raise RuntimeError(fail_reason)
            _magick_to_jpeg(rawtherapee_tif, output_path, quality, max_side)
            _validate_output_file(output_path)
            fail_reason = _image_fail_reason(output_path)
            if fail_reason:
                raise RuntimeError(fail_reason)
            logging.info("raw_step=rawtherapee-cli status=ok reason=decode_success")
            return
        except CommandExecutionError as exc:
            _record_fail("rawtherapee-cli", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("rawtherapee-cli", str(exc))
        except Exception as exc:
            _record_fail("rawtherapee-cli", str(exc))

    # C2) dcraw_emu -> TIFF, then magick -> JPG
    if shutil.which("dcraw_emu") is None:
        _record_fail("dcraw_emu", "command not found")
    else:
        try:
            _run(["dcraw_emu", *decode_cmd], timeout=DCRAW_TIMEOUT_SECONDS)
            generated = _find_decoded_raw_path(input_path)
            _validate_output_file(generated)
            fail_reason = _image_fail_reason(generated)
            if fail_reason:
                raise RuntimeError(fail_reason)
            _magick_to_jpeg(generated, output_path, quality, max_side)
            _validate_output_file(output_path)
            fail_reason = _image_fail_reason(output_path)
            if fail_reason:
                raise RuntimeError(fail_reason)
            logging.info("raw_step=dcraw_emu status=ok reason=decode_success")
            return
        except CommandExecutionError as exc:
            _record_fail("dcraw_emu", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("dcraw_emu", str(exc))
        except Exception as exc:
            _record_fail("dcraw_emu", str(exc))

    # C3) dcraw -> TIFF, then magick -> JPG
    if shutil.which("dcraw") is None:
        _record_fail("dcraw", "command not found")
    else:
        try:
            _run(["dcraw", *decode_cmd], timeout=DCRAW_TIMEOUT_SECONDS)
            generated = _find_decoded_raw_path(input_path)
            _validate_output_file(generated)
            fail_reason = _image_fail_reason(generated)
            if fail_reason:
                raise RuntimeError(fail_reason)
            _magick_to_jpeg(generated, output_path, quality, max_side)
            _validate_output_file(output_path)
            fail_reason = _image_fail_reason(output_path)
            if fail_reason:
                raise RuntimeError(fail_reason)
            logging.info("raw_step=dcraw status=ok reason=decode_success")
            return
        except CommandExecutionError as exc:
            _record_fail("dcraw", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("dcraw", str(exc))
        except Exception as exc:
            _record_fail("dcraw", str(exc))

    raise RuntimeError("RAW conversion failed; " + _format_raw_errors(errors))


def _convert_heif_with_fallback(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    if shutil.which("heif-convert") is not None:
        try:
            heif_tmp_jpg = input_path.with_name("heif_fallback.jpg")
            _run(["heif-convert", str(input_path), str(heif_tmp_jpg)], timeout=SUBPROCESS_TIMEOUT_SECONDS)
            _validate_output_file(heif_tmp_jpg)
            if not _image_ok(heif_tmp_jpg):
                raise RuntimeError("image check failed for heif-convert output")
            _magick_to_jpeg(heif_tmp_jpg, output_path, quality, max_side)
            _validate_output_file(output_path)
            if not _image_ok(output_path):
                raise RuntimeError("image check failed for output jpeg")
            return
        except (CommandExecutionError, RuntimeError) as exc:
            logging.warning("heif_step=heif-convert status=fail reason=%s falling_back_to_magick", exc)

    # Fallback: ImageMagick direct decode (works when libheif is installed)
    _magick_to_jpeg(input_path, output_path, quality, max_side)
    _validate_output_file(output_path)
    if not _image_ok(output_path):
        raise RuntimeError("image check failed for output jpeg")


async def _convert_raw_or_422(
    in_path: Path,
    out_path: Path,
    quality: int,
    max_side: Optional[int],
) -> None:
    try:
        await asyncio.to_thread(_convert_raw, in_path, out_path, quality, max_side)
        _validate_output_file(out_path)
        if not await asyncio.to_thread(_image_ok, out_path):
            raise RuntimeError("image check failed for output jpeg")
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=_truncate_stderr(str(exc))) from exc


def _success_response(
    out_path: Path,
    tmpdir: Path,
    suffix: str,
    size_bytes: int,
    quality: int,
    max_side: Optional[int],
    start: float,
) -> FileResponse:
    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
    logging.info(
        "status=ok ext=%s in_bytes=%d out_bytes=%d quality=%d max_side=%s elapsed_ms=%s",
        suffix, size_bytes, out_path.stat().st_size, quality, max_side, elapsed_ms,
    )
    return FileResponse(
        path=out_path,
        media_type="image/jpeg",
        filename="output.jpg",
        background=BackgroundTask(lambda: shutil.rmtree(tmpdir, ignore_errors=True)),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _perform_conversion(
    in_path: Path,
    out_path: Path,
    quality: int,
    max_side: Optional[int],
) -> Path:
    """Detect the file type and convert in_path to a JPEG at out_path.

    Shared by the /convert HTTP route and the Pub/Sub job handler so a file
    only ever needs to be written to disk once, in one service.
    """
    input_size = in_path.stat().st_size

    try:
        file_type, mime_type = await asyncio.to_thread(_detect_filetype, in_path)
        route = _decoder_route(file_type, mime_type)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_truncate_stderr(str(exc))) from exc

    mapped_ext = _mapped_extension(file_type, mime_type)
    if mapped_ext and in_path.suffix.lower() != mapped_ext:
        renamed_path = in_path.with_name(f"input{mapped_ext}")
        in_path = in_path.rename(renamed_path)

    logging.info(
        "input_saved path=%s size=%d filetype=%s mimetype=%s",
        in_path, input_size, file_type, mime_type,
    )
    logging.info(
        "detect_filetype file_type=%s mime_type=%s route=%s",
        file_type, mime_type, route,
    )

    if route == "raw":
        if input_size < MIN_INPUT_BYTES:
            raise HTTPException(
                status_code=422,
                detail=f"RAW input too small: {input_size} bytes (min {MIN_INPUT_BYTES})",
            )
        logging.info("raw_input path=%s input_size=%d", in_path, input_size)
        await _convert_raw_or_422(in_path, out_path, quality, max_side)
        return out_path

    try:
        if route == "heif":
            await asyncio.to_thread(_convert_heif_with_fallback, in_path, out_path, quality, max_side)
        else:
            await asyncio.to_thread(_magick_to_jpeg, in_path, out_path, quality, max_side)
        _validate_output_file(out_path)
        if not await asyncio.to_thread(_image_ok, out_path):
            raise RuntimeError("image check failed for output jpeg")
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=_truncate_stderr(f"conversion failed: {exc}")) from exc

    return out_path


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    quality: int = Form(default=92),
    max_side: Optional[int] = Form(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> Response:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="converter API key is not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

    orig_name = file.filename or "upload"
    suffix = Path(orig_name).suffix.lower()

    if quality < 1 or quality > 100:
        raise HTTPException(status_code=400, detail="quality must be in range 1..100")
    if max_side is not None and max_side < 1:
        raise HTTPException(status_code=400, detail="max_side must be > 0")

    start = time.monotonic()
    content = await file.read()
    size_bytes = len(content)
    if size_bytes > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"file too large: max {MAX_FILE_MB}MB")

    tmpdir = Path(tempfile.mkdtemp(prefix="convert-"))
    effective_suffix = suffix or ".bin"
    in_path = tmpdir / f"input{effective_suffix}"
    out_path = tmpdir / "output.jpg"

    try:
        with open(in_path, "wb") as tmp_in:
            tmp_in.write(content)
        out_path = await _perform_conversion(in_path, out_path, quality, max_side)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    return _success_response(out_path, tmpdir, suffix, size_bytes, quality, max_side, start)


@app.post("/pubsub/push")
async def pubsub_push(request: Request) -> JSONResponse:
    """Handle Pub/Sub push messages: download from Telegram, convert, upload back.

    Runs in-process (no HTTP hop to a separate converter service), so the raw
    file bytes cross the network exactly once on the way in and once on the way out.
    """
    if _bot is None or not CHAT_ID or not TOPIC_CONVERTED_ID:
        logging.error("Service not configured for Pub/Sub processing (BOT_TOKEN/CHAT_ID/TOPIC_CONVERTED_ID)")
        raise HTTPException(status_code=503, detail="pubsub processing not configured")

    try:
        body = await request.json()
    except Exception as exc:
        logging.exception("Failed to parse request body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    message = body.get("message", {})
    data_b64 = message.get("data")

    if not data_b64:
        logging.warning("No data in Pub/Sub message")
        return JSONResponse({"status": "ignored", "reason": "no_data"}, status_code=200)

    try:
        data_json = base64.b64decode(data_b64).decode("utf-8")
        job = json.loads(data_json)
    except Exception as exc:
        logging.exception("Failed to decode job data: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid job data") from exc

    file_id = job.get("file_id")
    file_unique_id = job.get("file_unique_id")
    chat_id = job.get("chat_id")
    message_id = job.get("message_id")
    file_name = _safe_filename(job.get("file_name"), fallback=file_id)

    if not file_id or not chat_id or not message_id:
        logging.warning("Missing required fields in job: %s", job)
        return JSONResponse({"status": "ignored", "reason": "missing_fields"}, status_code=200)

    idempotency_key = file_unique_id or f"{chat_id}:{message_id}"
    if idempotency_key in _processed_jobs:
        logging.info("Job already processed: %s", idempotency_key)
        return JSONResponse({"status": "duplicate", "key": idempotency_key}, status_code=200)

    # Claim before processing to prevent concurrent duplicate execution
    _processed_jobs[idempotency_key] = None

    try:
        await process_conversion_job(file_id=file_id, file_name=file_name or file_id, chat_id=chat_id)

        if len(_processed_jobs) > 10000:
            for key in list(_processed_jobs)[:5000]:
                del _processed_jobs[key]

        logging.info("Job completed successfully: %s", idempotency_key)
        return JSONResponse({"status": "success", "key": idempotency_key}, status_code=200)

    except TelegramBadRequest as exc:
        if _is_file_too_big_error(exc):
            logging.warning("ACK job due to Telegram size limit: %s", exc)
            try:
                await _tg_retry(_bot.send_message, chat_id=chat_id, message_thread_id=TOPIC_CONVERTED_ID, text="Файл слишком большой, лимит 20MB у Bot API")
            except Exception as notify_exc:  # noqa: BLE001
                logging.warning("Failed to notify chat about 20MB limit: %s", notify_exc)
            return JSONResponse(
                {"status": "skipped", "reason": "telegram_file_too_big", "key": idempotency_key},
                status_code=200,
            )
        del _processed_jobs[idempotency_key]
        logging.exception("Job processing failed with TelegramBadRequest: %s", exc)
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc
    except HTTPException as exc:
        if 400 <= exc.status_code < 500:
            logging.warning("ACK job due to conversion client error: %s", exc.detail)
            try:
                await _tg_retry(
                    _bot.send_message,
                    chat_id=chat_id,
                    message_thread_id=TOPIC_CONVERTED_ID,
                    text="Не удалось сконвертировать файл: формат не поддерживается",
                )
            except Exception as notify_exc:  # noqa: BLE001
                logging.warning("Failed to notify chat about unsupported format: %s", notify_exc)
            return JSONResponse(
                {"status": "skipped", "reason": "conversion_client_error", "key": idempotency_key},
                status_code=200,
            )
        del _processed_jobs[idempotency_key]
        logging.exception("Job processing failed with conversion server error: %s", exc.detail)
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc.detail}") from exc
    except Exception as exc:
        del _processed_jobs[idempotency_key]
        logging.exception("Job processing failed: %s", exc)
        # Return 5xx to trigger Pub/Sub retry
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc


async def process_conversion_job(file_id: str, file_name: str, chat_id: int) -> None:
    """Download from Telegram, convert, and upload the result back."""
    total_started = time.monotonic()
    tg_download_s: float | None = None
    convert_s: float | None = None
    tg_upload_s: float | None = None
    in_bytes = 0
    out_bytes = 0

    try:
        with tempfile.TemporaryDirectory(prefix="job-") as tmpdir:
            source = Path(tmpdir) / file_name
            out_path = Path(tmpdir) / "output.jpg"

            download_started = time.monotonic()
            file_info = await _tg_retry(_bot.get_file, file_id)
            await _tg_retry(_bot.download_file, file_info.file_path, destination=source)
            tg_download_s = time.monotonic() - download_started
            in_bytes = source.stat().st_size

            logging.info(
                "tg_download file=%s file_id=%s size=%s download_ms=%s",
                file_name, file_id, in_bytes, format_ms(tg_download_s)
            )

            convert_started = time.monotonic()
            out_path = await _perform_conversion(source, out_path, CONVERSION_QUALITY, None)
            convert_s = time.monotonic() - convert_started
            jpg_bytes = await asyncio.to_thread(out_path.read_bytes)
            out_bytes = len(jpg_bytes)

            logging.info(
                "conversion_done file=%s in_bytes=%s out_bytes=%s convert_ms=%s",
                file_name, in_bytes, out_bytes, format_ms(convert_s)
            )

            target_name = f"{Path(file_name).stem}.jpg"
            upload_started = time.monotonic()

            await _tg_retry(
                _bot.send_document,
                chat_id=CHAT_ID,
                message_thread_id=TOPIC_CONVERTED_ID,
                document=BufferedInputFile(jpg_bytes, filename=target_name),
            )
            tg_upload_s = time.monotonic() - upload_started

            total_s = time.monotonic() - total_started

            logging.info(
                "job_success file=%s file_id=%s chat_id=%s tg_download_ms=%s "
                "convert_ms=%s tg_upload_ms=%s total_ms=%s in_bytes=%s out_bytes=%s",
                file_name, file_id, chat_id,
                format_ms(tg_download_s),
                format_ms(convert_s),
                format_ms(tg_upload_s),
                format_ms(total_s),
                in_bytes,
                out_bytes,
            )

    except Exception as exc:
        total_s = time.monotonic() - total_started
        logging.error(
            "job_failed file=%s file_id=%s chat_id=%s tg_download_ms=%s "
            "convert_ms=%s tg_upload_ms=%s total_ms=%s error=%s",
            file_name, file_id, chat_id,
            format_ms(tg_download_s),
            format_ms(convert_s),
            format_ms(tg_upload_s),
            format_ms(total_s),
            str(exc),
        )
        raise


async def _check_tools() -> None:
    missing = [tool for tool in ("magick",) if shutil.which(tool) is None]
    if shutil.which("exiftool") is None:
        missing.append("exiftool")
    if shutil.which("heif-convert") is None:
        missing.append("heif-convert")
    if shutil.which("dcraw_emu") is None and shutil.which("dcraw") is None:
        missing.append("dcraw_emu|dcraw")
    if shutil.which("darktable-cli") is None:
        missing.append("darktable-cli")
    if shutil.which("rawtherapee-cli") is None:
        missing.append("rawtherapee-cli")

    # Check for libheif support in ImageMagick
    try:
        result, _ = _run(["magick", "-list", "format"], return_stderr=True)
        formats = result.decode("utf-8", errors="ignore")
        if "HEIC" not in formats and "HEIF" not in formats:
            missing.append("libheif(HEIC/HEIF)")
    except RuntimeError:
        pass

    if missing:
        logging.warning("missing_tools tools=%s", ",".join(missing))
