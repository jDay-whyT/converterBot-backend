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

ALLOWED_SUFFIXES = {".heic", ".dng", ".webp", ".tif", ".tiff"}

app = FastAPI(title="converter-service")


def _run(cmd: list[str], input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
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


def _convert_dng(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    raw = _run(["dcraw", "-c", "-w", "-q", "3", "-H", "0", str(input_path)])
    cmd = ["magick", "-", "-auto-orient", "-colorspace", "sRGB"]
    if max_side:
        cmd.extend(["-resize", f"{max_side}x{max_side}>"])
    cmd.extend(["-quality", str(quality), "-strip", str(output_path)])
    _run(cmd, input_bytes=raw)


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
            if suffix == ".dng":
                _convert_dng(in_path, out_path, quality, max_side)
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
    missing = [tool for tool in ("magick", "dcraw") if shutil.which(tool) is None]
    if missing:
        print(f"warning=missing_tools tools={','.join(missing)}", flush=True)
