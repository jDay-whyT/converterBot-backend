import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
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
        parts.append(f"{err.tool} rc={returncode}{timeout} err={err.stderr}")
    return " | ".join(parts)


def _convert_raw(input_path: Path, output_path: Path, quality: int, max_side: Optional[int]) -> None:
    errors: list[CommandError] = []
    decode_cmd = ["-T", "-w", "-q", "3", "-H", "0", str(input_path)]

    # 1) dcraw_emu -> TIFF, then magick -> JPG
    if shutil.which("dcraw_emu") is None:
        errors.append(CommandError(tool="dcraw_emu", returncode=None, stderr="command not found", timeout=False))
    else:
        try:
            _run(["dcraw_emu", *decode_cmd], timeout=DCRAW_TIMEOUT_SECONDS)
            generated = _find_decoded_raw_path(input_path)
            _magick_to_jpeg(generated, output_path, quality, max_side)
            return
        except CommandExecutionError as exc:
            print(
                f"raw_step=dcraw_emu status=fail timeout={int(exc.timeout)} rc={exc.returncode} stderr={exc.stderr}",
                flush=True,
            )
            errors.append(CommandError(tool="dcraw_emu", returncode=exc.returncode, stderr=exc.stderr, timeout=exc.timeout))
        except Exception as exc:
            stderr = _truncate_stderr(str(exc))
            print(f"raw_step=dcraw_emu status=fail timeout=0 rc=na stderr={stderr}", flush=True)
            errors.append(CommandError(tool="dcraw_emu", returncode=None, stderr=stderr, timeout=False))

    # 2) dcraw -> TIFF, then magick -> JPG
    if shutil.which("dcraw") is None:
        errors.append(CommandError(tool="dcraw", returncode=None, stderr="command not found", timeout=False))
    else:
        try:
            _run(["dcraw", *decode_cmd], timeout=DCRAW_TIMEOUT_SECONDS)
            generated = _find_decoded_raw_path(input_path)
            _magick_to_jpeg(generated, output_path, quality, max_side)
            return
        except CommandExecutionError as exc:
            print(f"raw_step=dcraw status=fail timeout={int(exc.timeout)} rc={exc.returncode} stderr={exc.stderr}", flush=True)
            errors.append(CommandError(tool="dcraw", returncode=exc.returncode, stderr=exc.stderr, timeout=exc.timeout))
        except Exception as exc:
            stderr = _truncate_stderr(str(exc))
            print(f"raw_step=dcraw status=fail timeout=0 rc=na stderr={stderr}", flush=True)
            errors.append(CommandError(tool="dcraw", returncode=None, stderr=stderr, timeout=False))

    # 3) darktable-cli -> jpg directly
    if shutil.which("darktable-cli") is None:
        errors.append(CommandError(tool="darktable-cli", returncode=None, stderr="command not found", timeout=False))
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
            _magick_to_jpeg(darktable_jpg, output_path, quality, max_side)
            return
        except CommandExecutionError as exc:
            print(
                f"raw_step=darktable-cli status=fail timeout={int(exc.timeout)} rc={exc.returncode} stderr={exc.stderr}",
                flush=True,
            )
            errors.append(
                CommandError(tool="darktable-cli", returncode=exc.returncode, stderr=exc.stderr, timeout=exc.timeout)
            )
        except Exception as exc:
            stderr = _truncate_stderr(str(exc))
            print(f"raw_step=darktable-cli status=fail timeout=0 rc=na stderr={stderr}", flush=True)
            errors.append(CommandError(tool="darktable-cli", returncode=None, stderr=stderr, timeout=False))

    raise RuntimeError("RAW conversion failed; " + _format_raw_errors(errors))


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

    with tempfile.TemporaryDirectory(prefix="convert-") as tmpdir:
        effective_suffix = suffix or (".dng" if is_raw else ".bin")
        in_path = Path(tmpdir) / f"input{effective_suffix}"
        out_path = Path(tmpdir) / "output.jpg"
        in_path.write_bytes(content)

        try:
            if is_raw:
                _convert_raw(in_path, out_path, quality, max_side)
            else:
                _magick_to_jpeg(in_path, out_path, quality, max_side)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=_truncate_stderr(f"conversion failed: {exc}")) from exc

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
        result, _ = _run(["magick", "-list", "format"], return_stderr=True)
        formats = result.decode("utf-8", errors="ignore")
        if "HEIC" not in formats and "HEIF" not in formats:
            missing.append("libheif(HEIC/HEIF)")
    except RuntimeError:
        pass

    if missing:
        print(f"warning=missing_tools tools={','.join(missing)}", flush=True)
