import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response

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

app = FastAPI(title="converter-service")


def _run(cmd: list[str], input_bytes: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"command not found: {cmd[0]}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(stderr or f"command failed: {' '.join(cmd)}")
    return proc.stdout


def _convert_with_magick(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    cmd = [
        "magick",
        str(input_path),
        "-auto-orient",
        "-colorspace",
        "sRGB",
    ]
    if max_side:
        cmd.extend(["-resize", f"{max_side}x{max_side}>"])
    cmd.extend(["-quality", str(quality), "-strip", str(output_path)])
    _run(cmd)


def _magick_to_jpeg(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    cmd = ["magick", str(input_path), "-auto-orient", "-colorspace", "sRGB"]
    if max_side:
        cmd.extend(["-resize", f"{max_side}x{max_side}>"])
    cmd.extend(["-quality", str(quality), "-strip", str(output_path)])
    _run(cmd)


def _convert_raw(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    errors: list[str] = []
    decode_cmd = ["-T", "-w", "-q", "3", "-H", "0", str(input_path)]

    # 1) dcraw_emu -> TIFF, then magick -> JPG
    dcraw_emu_tiff = input_path.with_name("raw_dcraw_emu.tiff")
    if shutil.which("dcraw_emu") is None:
        errors.append("dcraw_emu: command not found")
    else:
        try:
            _run(["dcraw_emu", *decode_cmd])
            generated = input_path.with_suffix(".tiff")
            if not generated.exists():
                raise RuntimeError(f"expected decoded TIFF not found: {generated}")
            if generated != dcraw_emu_tiff:
                generated.rename(dcraw_emu_tiff)
            _magick_to_jpeg(dcraw_emu_tiff, output_path, quality, max_side)
            return
        except Exception as exc:
            stderr = str(exc).strip()
            print(f"raw_step=dcraw_emu status=fail reason={stderr}", flush=True)
            errors.append(f"dcraw_emu: {stderr}")

    # 2) dcraw -> TIFF, then magick -> JPG
    dcraw_tiff = input_path.with_name("raw_dcraw.tiff")
    if shutil.which("dcraw") is None:
        errors.append("dcraw: command not found")
    else:
        try:
            _run(["dcraw", *decode_cmd])
            generated = input_path.with_suffix(".tiff")
            if not generated.exists():
                raise RuntimeError(f"expected decoded TIFF not found: {generated}")
            if generated != dcraw_tiff:
                generated.rename(dcraw_tiff)
            _magick_to_jpeg(dcraw_tiff, output_path, quality, max_side)
            return
        except Exception as exc:
            stderr = str(exc).strip()
            print(f"raw_step=dcraw status=fail reason={stderr}", flush=True)
            errors.append(f"dcraw: {stderr}")

    # 3) darktable-cli -> jpg directly
    if shutil.which("darktable-cli") is None:
        errors.append("darktable-cli: command not found")
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
            ])
            _magick_to_jpeg(darktable_jpg, output_path, quality, max_side)
            return
        except Exception as exc:
            stderr = str(exc).strip()
            print(f"raw_step=darktable status=fail reason={stderr}", flush=True)
            errors.append(f"darktable-cli: {stderr}")

    raise RuntimeError("RAW conversion failed; " + " | ".join(errors))


def _is_raw_upload(suffix: str, content_type: Optional[str]) -> bool:
    if suffix in RAW_SUFFIXES:
        return True
    if not content_type:
        return False
    ct = content_type.lower()
    return ct in RAW_MIME_TYPES or ct.startswith(RAW_MIME_PREFIXES)


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
    if suffix not in ALLOWED_SUFFIXES:
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

    with tempfile.TemporaryDirectory(prefix="convert-") as tmpdir:
        in_path = Path(tmpdir) / f"input{suffix}"
        out_path = Path(tmpdir) / "output.jpg"
        in_path.write_bytes(content)

        try:
            if _is_raw_upload(suffix, file.content_type):
                _convert_raw(in_path, out_path, quality, max_side)
            else:
                _convert_with_magick(in_path, out_path, quality, max_side)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=f"conversion failed: {exc}") from exc

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="conversion failed: empty output")

        output = out_path.read_bytes()

    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
    print(
        f"status=ok ext={suffix} in_bytes={size_bytes} out_bytes={len(output)} quality={quality} "
        f"max_side={max_side} elapsed_ms={elapsed_ms}",
        flush=True,
    )

    return Response(content=output, media_type="image/jpeg")


@app.on_event("startup")
def _check_tools() -> None:
    missing = [tool for tool in ("magick",) if shutil.which(tool) is None]
    if shutil.which("dcraw_emu") is None and shutil.which("dcraw") is None:
        missing.append("dcraw_emu|dcraw")
    if shutil.which("darktable-cli") is None:
        missing.append("darktable-cli")

    # Check for libheif support in ImageMagick
    try:
        result = _run(["magick", "-list", "format"])
        formats = result.decode("utf-8", errors="ignore")
        if "HEIC" not in formats and "HEIF" not in formats:
            missing.append("libheif(HEIC/HEIF)")
    except (RuntimeError, FileNotFoundError):
        pass

    if missing:
        print(f"warning=missing_tools tools={','.join(missing)}", flush=True)
