"""Getting a mix onto disk, splitting it into stems, and reading clips back out.

Every file lives in a per-mix work folder so a rerun skips steps that already finished:

    source.*          the downloaded audio, as YouTube served it
    mix.flac          16 kHz mono, what gets fingerprinted
    vocals.flac       16 kHz mono vocal stem (Demucs)
    instrumental.flac 16 kHz mono everything-but-vocals stem (Demucs)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

QUERY_RATE = 16_000
STEM_CHUNK_SECONDS = 600
SOURCES = ("mix", "vocals", "instrumental")

Progress = Callable[[str], None]


class AudioError(RuntimeError):
    pass


def cache_root() -> Path:
    if env := os.environ.get("TRACKLIST_MCP_CACHE"):
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tracklist-mcp"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "tracklist-mcp" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tracklist-mcp"


@dataclass
class MixInfo:
    id: str
    title: str
    url: str | None
    uploader: str | None
    duration: float
    description: str | None = None


_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")


def mix_id(source: str) -> str:
    """A stable folder name for a link or a local file."""
    if m := _YT_ID.search(source):
        return f"yt-{m.group(1)}"
    path = Path(source).expanduser()
    if path.exists():
        st = path.stat()
        digest = hashlib.sha1(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]
        return f"file-{digest}"
    return "url-" + hashlib.sha1(source.encode()).hexdigest()[:12]


def require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioError("ffmpeg isn't installed. Install it (macOS: `brew install ffmpeg`) and try again.")
    return exe


def _ffmpeg(*args: str) -> None:
    proc = subprocess.run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True)
    if proc.returncode:
        raise AudioError(f"ffmpeg failed: {proc.stderr.decode(errors='replace').strip()[-500:]}")


def _js_runtimes() -> dict[str, dict]:
    """YouTube needs a JavaScript runtime to unlock most formats; use whichever is installed."""
    return {name: {} for name in ("deno", "node", "bun") if shutil.which(name)} or {"deno": {}}


def fetch(source: str, work: Path, progress: Progress) -> MixInfo:
    """Download (or copy) the mix and write the 16 kHz mono file used for fingerprinting."""
    work.mkdir(parents=True, exist_ok=True)
    info_path = work / "info.json"
    if info_path.exists() and (work / "mix.flac").exists():
        return MixInfo(**json.loads(info_path.read_text()))

    local = Path(source).expanduser()
    if local.exists():
        src = local
        info = MixInfo(id=mix_id(source), title=local.stem, url=None, uploader=None, duration=0.0)
    else:
        src, info = _download(source, work, progress)

    progress("Decoding audio")
    _ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", str(QUERY_RATE), "-sample_fmt", "s16", str(work / "mix.flac"))
    info.duration = sf.info(str(work / "mix.flac")).duration
    info_path.write_text(json.dumps(asdict(info), indent=1))
    return info


def _download(url: str, work: Path, progress: Progress) -> tuple[Path, MixInfo]:
    import yt_dlp

    last = [-1]

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = int(100 * d.get("downloaded_bytes", 0) / total)
                if pct // 10 != last[0]:
                    last[0] = pct // 10
                    progress(f"Downloading audio: {pct}%")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(work / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "js_runtimes": _js_runtimes(),
    }
    progress("Downloading audio")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise AudioError(f"Couldn't download {url}: {e}") from e
    if meta.get("_type") == "playlist":
        raise AudioError("That link is a playlist. Paste the link to a single video.")
    files = [p for p in work.glob("source.*") if not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise AudioError(f"yt-dlp finished but no audio file appeared in {work}")
    info = MixInfo(
        id=mix_id(url),
        title=meta.get("title") or url,
        url=meta.get("webpage_url") or url,
        uploader=meta.get("uploader") or meta.get("channel"),
        duration=float(meta.get("duration") or 0),
        description=meta.get("description"),
    )
    return files[0], info


def _demucs_device() -> str:
    try:
        import torch
    except ImportError as e:
        raise AudioError("Demucs isn't installed; reinstall tracklist-mcp with its dependencies.") from e
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def separate(work: Path, progress: Progress) -> None:
    """Split the mix into vocals and instrumental with Demucs.

    Long mixes are cut into chunks first: Demucs holds the whole input (and four output stems) in memory,
    which for a two-hour set is several gigabytes. A seam every ten minutes doesn't matter for fingerprinting.
    """
    if all((work / f"{s}.flac").exists() for s in ("vocals", "instrumental")):
        return
    src = next((p for p in work.glob("source.*") if p.suffix not in (".part", ".ytdl")), None) or work / "mix.flac"
    device = _demucs_device()
    duration = sf.info(str(work / "mix.flac")).duration
    n_chunks = max(1, int(np.ceil(duration / STEM_CHUNK_SECONDS)))

    with tempfile.TemporaryDirectory(dir=work) as tmp_str:
        tmp = Path(tmp_str)
        parts: dict[str, list[Path]] = {"vocals": [], "no_vocals": []}
        for i in range(n_chunks):
            progress(f"Separating vocals from the music ({i + 1}/{n_chunks}, on {device})")
            chunk = tmp / f"chunk{i:03d}.wav"
            _ffmpeg(
                "-ss", str(i * STEM_CHUNK_SECONDS), "-t", str(STEM_CHUNK_SECONDS), "-i", str(src),
                "-vn", "-ac", "2", "-ar", "44100", str(chunk),
            )  # fmt: skip
            cmd = [
                sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
                "-d", device, "-o", str(tmp / "out"), "--filename", "{track}_{stem}.{ext}", str(chunk),
            ]  # fmt: skip
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode and device != "cpu":
                device = "cpu"  # some ops are missing on MPS in older torch builds
                cmd[cmd.index("-d") + 1] = device
                proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode:
                raise AudioError(f"Demucs failed: {proc.stderr.decode(errors='replace').strip()[-800:]}")
            for stem in parts:
                parts[stem].append(tmp / "out" / "htdemucs" / f"chunk{i:03d}_{stem}.wav")
            chunk.unlink()

        for stem, name in (("vocals", "vocals"), ("no_vocals", "instrumental")):
            listing = tmp / f"{stem}.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts[stem]))
            _ffmpeg(
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-ac", "1", "-ar", str(QUERY_RATE), "-sample_fmt", "s16", str(work / f"{name}.flac"),
            )  # fmt: skip


def clip(path: Path, start: float, seconds: float) -> bytes:
    """`seconds` of audio from `start`, as WAV bytes ready for fingerprinting."""
    with sf.SoundFile(str(path)) as f:
        f.seek(max(0, int(start * f.samplerate)))
        data = f.read(int(seconds * f.samplerate), dtype="int16")
        rate = f.samplerate
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def loudness(path: Path, step: float = 1.0) -> np.ndarray:
    """RMS level per `step` seconds, as a float array (linear, full scale = 1)."""
    out = []
    with sf.SoundFile(str(path)) as f:
        block = int(f.samplerate * step)
        for data in f.blocks(blocksize=block, dtype="float32"):
            out.append(float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0)
    return np.asarray(out)


def read_seconds(path: Path, start: float, seconds: float) -> np.ndarray:
    with sf.SoundFile(str(path)) as f:
        f.seek(max(0, int(start * f.samplerate)))
        return f.read(int(seconds * f.samplerate), dtype="float32")


def cleanup(work: Path) -> None:
    """Delete the large audio files but keep what's needed to show the result again."""
    shutil.rmtree(work / "refs", ignore_errors=True)
    shutil.rmtree(work / "versions", ignore_errors=True)
    for p in work.iterdir():
        if p.suffix in (".flac", ".wav", ".m4a", ".webm", ".opus", ".mp3", ".mp4", ".part") or p.name.startswith(
            "source."
        ):
            p.unlink(missing_ok=True)
