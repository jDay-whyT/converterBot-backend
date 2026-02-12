import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

API_KEY = os.getenv("CONVERTER_API_KEY", "")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "40"))

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

SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv("SUBPROCESS_TIMEOUT_SECONDS", "90"))
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
MIN_MEAN_LUMA = float(os.getenv("MIN_MEAN_LUMA", "0.02"))
MIN_REGION_MEAN_LUMA = float(os.getenv("MIN_REGION_MEAN_LUMA", "0.015"))

app = FastAPI(title="converter-service")


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


def _mean_luma(path: Path) -> float:
    try:
        result = _run(
            [
                "magick",
                str(path),
                "-colorspace",
                "Gray",
                "-resize",
                "64x64!",
                "-format",
                "%[fx:mean]",
                "info:",
            ],
            timeout=MAGICK_TIMEOUT_SECONDS,
        )
        return float(result.decode("utf-8", errors="ignore").strip())
    except Exception:
        return -1.0


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


def _bands_ok(path: Path) -> bool:
    full = _region_luma(path, "100%x100%")
    left = _region_luma(path, "50%x100%+0+0", gravity="West")
    right = _region_luma(path, "50%x100%+0+0", gravity="East")
    top = _region_luma(path, "100%x50%+0+0", gravity="North")
    bottom = _region_luma(path, "100%x50%+0+0", gravity="South")

    means = (full, left, right, top, bottom)
    ok = all(mean >= MIN_REGION_MEAN_LUMA for mean in means)
    mean_str = lambda val: f"{val:.6f}" if val >= 0 else "na"
    print(
        "img_check "
        f"mean_full={mean_str(full)} mean_l={mean_str(left)} mean_r={mean_str(right)} "
        f"mean_t={mean_str(top)} mean_b={mean_str(bottom)} ok={int(ok)}",
        flush=True,
    )
    return ok


def _image_fail_reason(path: Path, min_dimension: int = 200) -> Optional[str]:
    if not _identify_ok(path, min_dimension=min_dimension):
        return "identify_failed"

    mean = _mean_luma(path)
    if mean < MIN_MEAN_LUMA:
        return "mean_failed"

    if not _bands_ok(path):
        return "band_check_failed"

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
        parts.append(f"raw_step={err.tool} rc={returncode}{timeout} stderr={err.stderr}")
    return " | ".join(parts)


def _convert_raw(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    errors: list[CommandError] = []
    decode_cmd = ["-T", "-w", "-q", "3", "-H", "0", str(input_path)]

    def _record_fail(tool: str, reason: str, returncode: Optional[int] = None, timeout: bool = False) -> None:
        stderr = _truncate_stderr(reason)
        rc = "na" if returncode is None else str(returncode)
        print(f"raw_step={tool} status=fail reason={stderr} timeout={int(timeout)} rc={rc}", flush=True)
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
                        timeout=SUBPROCESS_TIMEOUT_SECONDS,
                        env={**os.environ, **DEFAULT_SUBPROCESS_ENV},
                    )
                stderr = _truncate_stderr(proc.stderr.decode("utf-8", errors="ignore") or "")
                if proc.returncode != 0:
                    raise CommandExecutionError("exiftool", proc.returncode, stderr or "preview extraction failed")
                _validate_output_file(preview_path)
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
                print(f"raw_step=exiftool:{preview_tag} status=ok reason=preview_extracted", flush=True)
                return
            except CommandExecutionError as exc:
                _record_fail(f"exiftool:{preview_tag}", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
                preview_path.unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                _record_fail(f"exiftool:{preview_tag}", f"timeout after {SUBPROCESS_TIMEOUT_SECONDS}s", timeout=True)
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
            fail_reason = _image_fail_reason(darktable_jpg)
            if fail_reason:
                raise RuntimeError(fail_reason)
            _magick_to_jpeg(darktable_jpg, output_path, quality, max_side)
            _validate_output_file(output_path)
            fail_reason = _image_fail_reason(output_path)
            if fail_reason:
                raise RuntimeError(fail_reason)
            print("raw_step=darktable-cli status=ok reason=render_success", flush=True)
            return
        except CommandExecutionError as exc:
            _record_fail("darktable-cli", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("darktable-cli", str(exc))
        except Exception as exc:
            _record_fail("darktable-cli", str(exc))

    # C1) dcraw_emu -> TIFF, then magick -> JPG
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
            print("raw_step=dcraw_emu status=ok reason=decode_success", flush=True)
            return
        except CommandExecutionError as exc:
            _record_fail("dcraw_emu", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("dcraw_emu", str(exc))
        except Exception as exc:
            _record_fail("dcraw_emu", str(exc))

    # C2) dcraw -> TIFF, then magick -> JPG
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
            print("raw_step=dcraw status=ok reason=decode_success", flush=True)
            return
        except CommandExecutionError as exc:
            _record_fail("dcraw", exc.stderr, returncode=exc.returncode, timeout=exc.timeout)
        except RuntimeError as exc:
            _record_fail("dcraw", str(exc))
        except Exception as exc:
            _record_fail("dcraw", str(exc))

    raise RuntimeError("RAW conversion failed; " + _format_raw_errors(errors))


def _convert_heif_with_fallback(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    try:
        _magick_to_jpeg(input_path, output_path, quality, max_side)
        _validate_output_file(output_path)
        if not _image_ok(output_path):
            raise RuntimeError("image check failed for output jpeg")
        return
    except RuntimeError as exc:
        if shutil.which("heif-convert") is None:
            raise RuntimeError("HEIF decode failed and heif-convert is unavailable") from exc

    heif_tmp_jpg = input_path.with_name("heif_fallback.jpg")
    _run(["heif-convert", str(input_path), str(heif_tmp_jpg)], timeout=SUBPROCESS_TIMEOUT_SECONDS)
    _validate_output_file(heif_tmp_jpg)
    if not _image_ok(heif_tmp_jpg):
        raise RuntimeError("image check failed for heif-convert output")
    _magick_to_jpeg(heif_tmp_jpg, output_path, quality, max_side)
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
        _convert_raw(in_path, out_path, quality, max_side)
        _validate_output_file(out_path)
        if not _image_ok(out_path):
            raise RuntimeError("image check failed for output jpeg")
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=_truncate_stderr(f"RAW conversion failed: {exc}")) from exc


def _is_raw_upload(suffix: str, content_type: Optional[str]) -> bool:
    normalized_suffix = suffix.lower()
    if normalized_suffix in RAW_SUFFIXES:
        return True

    if normalized_suffix in (ALLOWED_SUFFIXES - RAW_SUFFIXES):
        return False

    if not content_type:
        return False

    ct = content_type.lower()
    return ct in RAW_MIME_TYPES or any(ct.startswith(prefix) for prefix in RAW_MIME_PREFIXES)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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

    filename = file.filename or "input.bin"
    suffix = Path(filename).suffix.lower()
    is_raw = _is_raw_upload(suffix, file.content_type)
    if suffix not in ALLOWED_SUFFIXES and not (not suffix and is_raw):
        raise HTTPException(status_code=400, detail="unsupported file extension")

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
    effective_suffix = suffix or (".dng" if is_raw else ".bin")
    in_path = tmpdir / f"input{effective_suffix}"
    out_path = tmpdir / "output.jpg"

    try:
        with open(in_path, "wb") as tmp_in:
            tmp_in.write(content)

        input_size = in_path.stat().st_size

        if is_raw:
            if input_size < MIN_INPUT_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"RAW input too small: {input_size} bytes (min {MIN_INPUT_BYTES})",
                )
            print(f"raw_input path={in_path} input_size={input_size}", flush=True)
            await _convert_raw_or_422(in_path, out_path, quality, max_side)
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            print(
                f"status=ok ext={suffix} in_bytes={size_bytes} out_bytes={out_path.stat().st_size} quality={quality} "
                f"max_side={max_side} elapsed_ms={elapsed_ms}",
                flush=True,
            )
            return FileResponse(
                path=out_path,
                media_type="image/jpeg",
                filename="output.jpg",
                background=BackgroundTask(lambda: shutil.rmtree(tmpdir, ignore_errors=True)),
            )
        else:
            try:
                if suffix in {".heic", ".heif"}:
                    _convert_heif_with_fallback(in_path, out_path, quality, max_side)
                else:
                    _magick_to_jpeg(in_path, out_path, quality, max_side)
                _validate_output_file(out_path)
                if not _image_ok(out_path):
                    raise RuntimeError("image check failed for output jpeg")
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=_truncate_stderr(f"conversion failed: {exc}")) from exc

        _validate_output_file(out_path)
        if not _image_ok(out_path):
            raise RuntimeError("image check failed for output jpeg")
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
    print(
        f"status=ok ext={suffix} in_bytes={size_bytes} out_bytes={out_path.stat().st_size} quality={quality} "
        f"max_side={max_side} elapsed_ms={elapsed_ms}",
        flush=True,
    )

    return FileResponse(
        path=out_path,
        media_type="image/jpeg",
        filename="output.jpg",
        background=BackgroundTask(lambda: shutil.rmtree(tmpdir, ignore_errors=True)),
    )


@app.on_event("startup")
def _check_tools() -> None:
    missing = [tool for tool in ("magick",) if shutil.which(tool) is None]
    if shutil.which("exiftool") is None:
        missing.append("exiftool")
    if shutil.which("heif-convert") is None:
        missing.append("heif-convert")
    if shutil.which("dcraw_emu") is None and shutil.which("dcraw") is None:
        missing.append("dcraw_emu|dcraw")
    if shutil.which("darktable-cli") is None:
        missing.append("darktable-cli")

    # Check for libheif support in ImageMagick
    try:
        result, _ = _run(["magick", "-list", "format"], return_stderr=True)
        formats = result.decode("utf-8", errors="ignore")
        if "HEIC" not in formats and "HEIF" not in formats:
            missing.append("libheif(HEIC/HEIF)")
    except RuntimeError:
        pass

    if missing:
        print(f"warning=missing_tools tools={','.join(missing)}", flush=True)
