"""Telling a vocal track from an acapella over another record, when Shazam can't name the music underneath.

Shazam knows many records only with their vocals on, so an instrumental stem often comes back unrecognised. Then
the question "is the music under this vocal the vocal's own record?" is answered directly: fetch the song, split
off its instrumental, and compare harmony (chroma) with the mix's instrumental stem at every alignment and at the
tempo and pitch changes a DJ might use. Its own record scores ~0.95+, a different record 0.3-0.65 (records in
the same key share a lot of harmony), a different version of the same song somewhere between. Only clear scores
count either way.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from . import audio
from .recognize import Match

RATE = audio.QUERY_RATE
HOP = 1600  # 0.1 s frames
NFFT = 4096
BINS = 36  # third-of-a-semitone pitch classes, so a record played a little sharp or flat still lines up
SAME_RECORD = 0.80
OTHER_RECORD = 0.50  # measured: acapellas over another record 0.32-0.47, remixes of the same song 0.56-0.62


def chroma(x: np.ndarray) -> np.ndarray:
    """Unit-length pitch-class profile (BINS per octave) per 0.1 s frame."""
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) < NFFT:
        return np.zeros((0, BINS), dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP] * np.hanning(NFFT)
    mag = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(NFFT, 1 / RATE)
    sel = (freqs > 55) & (freqs < 4000)
    pitch = (np.round(BINS * np.log2(freqs[sel] / 440)) % BINS).astype(int)
    c = np.zeros((len(frames), BINS))
    for k in range(BINS):
        c[:, k] = mag[:, sel][:, pitch == k].sum(axis=1)
    c = np.log1p(c * 100)
    c -= c.mean(axis=1, keepdims=True)
    return (c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-9)).astype(np.float32)


def similarity(query: np.ndarray, reference: np.ndarray, shifts: range | tuple = (-2, -1, 0, 1, 2)) -> float:
    """Best average frame similarity of `query` anywhere in `reference`.

    Tries tempo changes up to ±10 % and pitch shifts up to ±2/3 semitone (a record sped up 4 % without key lock).
    """
    best = -1.0
    if len(query) < 10:
        return best
    for scale in np.arange(0.90, 1.101, 0.02):
        n = int(len(query) * scale)
        if n < 10 or len(reference) < n:
            continue
        pos = np.linspace(0, len(query) - 1, n)
        q = np.stack([np.interp(pos, np.arange(len(query)), query[:, k]) for k in range(BINS)], axis=1)
        q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-9
        windows = np.lib.stride_tricks.sliding_window_view(reference, (n, BINS))[:, 0]
        for shift in shifts:
            score = np.einsum("lnk,nk->l", windows, np.roll(q, shift, axis=1)) / n
            best = max(best, float(score.max()))
    return best


def verdict(score: float) -> str:
    return "own" if score >= SAME_RECORD else "other" if score <= OTHER_RECORD else "unsure"


def _slug(m: Match) -> str:
    return re.sub(r"[^\w]+", "_", f"{m.artist}-{m.title}")[:80] + f"-{m.key}"


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _plausible(entry: dict, m: Match) -> bool:
    """A search result that is this song: its title words and an artist word in the upload's title."""
    from .timeline import song_id

    title = _words(entry.get("title") or "")
    wanted = _words(song_id(m.title))
    artist = _words(m.artist) - {"feat", "ft", "x", "and", "the"}
    duration = entry.get("duration") or 0
    return bool(wanted) and wanted <= title and bool(artist & title) and 60 < duration < 900


def reference_instrumental(m: Match, folder: Path) -> Path | None:
    """The instrumental of the released song, from its first plausible YouTube upload. None if unavailable."""
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{_slug(m)}.flac"
    failed = folder / f"{_slug(m)}.failed"
    if out.exists():
        return out
    if failed.exists():
        return None
    try:
        import yt_dlp

        with tempfile.TemporaryDirectory(dir=folder) as tmp_str:
            tmp = Path(tmp_str)
            opts = {
                "format": "bestaudio/best",
                "outtmpl": str(tmp / "song.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
                "max_filesize": 60_000_000,
                "match_filter": yt_dlp.utils.match_filter_func("duration > 60 & duration < 900"),
                "js_runtimes": audio._js_runtimes(),
            }
            with yt_dlp.YoutubeDL({**opts, "extract_flat": True}) as ydl:
                found = ydl.extract_info(f"ytsearch6:{m.artist} - {m.title}", download=False).get("entries") or []
            pick = next((e for e in found if _plausible(e, m)), None)
            if pick is None:
                raise audio.AudioError("no upload with this title")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(pick.get("url") or pick["id"], download=True)
            song = next((p for p in tmp.glob("song.*") if p.suffix not in (".part", ".ytdl")), None)
            if song is None:
                raise audio.AudioError("no download")
            wav = tmp / "song.wav"
            audio._ffmpeg("-i", str(song), "-t", "900", "-ac", "2", "-ar", "44100", str(wav))
            cmd = [
                sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
                "-d", audio._demucs_device(), "-o", str(tmp / "sep"), "--filename", "{stem}.{ext}", str(wav),
            ]  # fmt: skip
            if subprocess.run(cmd, capture_output=True).returncode:
                cmd[cmd.index("-d") + 1] = "cpu"
                if subprocess.run(cmd, capture_output=True).returncode:
                    raise audio.AudioError("demucs failed")
            audio._ffmpeg(
                "-i", str(tmp / "sep" / "htdemucs" / "no_vocals.wav"),
                "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(out),
            )  # fmt: skip
        return out
    except Exception:  # a missing reference only means "can't tell", never a failed run
        failed.touch()
        return None


def check_beds(windows: list[tuple[float, Match]], instrumental: Path, window: float, folder: Path) -> list[dict]:
    """For each (time, vocal match): how much the instrumental stem there sounds like the vocal's own record."""
    by_song: dict[str, list[tuple[float, Match]]] = {}
    for t, m in windows:
        by_song.setdefault(m.key, []).append((t, m))
    out: list[dict] = []
    for hits in by_song.values():
        ref = reference_instrumental(hits[0][1], folder)
        if ref is None:
            continue
        ref_chroma = chroma(sf.read(str(ref), dtype="float32")[0])
        for t, m in hits:
            score = similarity(chroma(audio.read_seconds(instrumental, t, window)), ref_chroma)
            out.append({"t": t, "key": m.key, "title": m.title, "score": round(score, 3)})
    return out


def judge(scores: list[float]) -> str | None:
    """Verdict from several windows of one stretch; None without enough of them to say."""
    if len(scores) < 3:
        return None
    return verdict(float(np.median(scores)))


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else []


def save(path: Path, beds: list[dict]) -> None:
    path.write_text(json.dumps(beds, indent=1))
