"""Naming the exact version of a record when Shazam only knows the original.

DJ sets are full of remixes and edits that aren't in Shazam's catalogue; Shazam then names the original (or
pieces of unrelated records, which shows as an "uncertain" play). Remixes are usually on YouTube, though. For each
track, the version Shazam named is fetched and compared with the mix; if it doesn't match, remixes of the song
are searched for and compared the same way, and one is accepted only if it clearly matches and clearly beats
the others.

Remixes of one song share its melody and chords, so harmony alone can't tell them apart; timbre and rhythm
(a mel spectrogram) can. Measured on a real set: the remix actually played scored chroma 0.88-0.97 and mel
0.92-0.97; other remixes of the same song at most 0.69 and 0.82.
"""

from __future__ import annotations

import json
import re
import tempfile
from itertools import pairwise
from pathlib import Path

import numpy as np
import soundfile as sf

from . import audio
from .timeline import Entry, song_id
from .verify import BINS, RATE, chroma, similarity

SAME_CHROMA = 0.80
SAME_MEL = 0.88
LISTED_MEL = 0.92  # sound match needed for the version the uploader's tracklist names
MEL_MARGIN = 0.05  # over the best candidate that isn't a match (measured gaps: 0.09-0.15)
MAX_CANDIDATES = 14


def mel(x: np.ndarray, bands: int = 40) -> np.ndarray:
    """Unit-length log-mel-like band energies per 0.1 s frame (timbre and rhythm, not pitch)."""
    nfft, hop = 2048, 1600
    if len(x) < nfft:
        return np.zeros((0, bands), dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop] * np.hanning(nfft)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1 / RATE)
    edges = np.geomspace(40, 7500, bands + 1)
    m = np.stack([power[:, (freqs >= a) & (freqs < b)].sum(axis=1) for a, b in pairwise(edges)], axis=1)
    m = np.log(m + 1e-6)
    m -= m.mean(axis=1, keepdims=True)
    return (m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)).astype(np.float32)


def _best_alignment(query: np.ndarray, reference: np.ndarray) -> float:
    best = -1.0
    if len(query) < 10:
        return best
    for scale in np.arange(0.92, 1.081, 0.02):
        n = int(len(query) * scale)
        if len(reference) < n:
            continue
        pos = np.linspace(0, len(query) - 1, n)
        q = np.stack([np.interp(pos, np.arange(len(query)), query[:, k]) for k in range(query.shape[1])], axis=1)
        q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-9
        windows = np.lib.stride_tricks.sliding_window_view(reference, (n, query.shape[1]))[:, 0]
        best = max(best, float((np.einsum("lnk,nk->l", windows, q) / n).max()))
    return best


def score(segments: list[np.ndarray], record: np.ndarray) -> tuple[float, float]:
    """Median (chroma, mel) similarity of mix segments to a record."""
    rc, rm = chroma(record), mel(record)
    # Any key: a remix uploaded (or played) transposed is still that remix; timbre tells remixes apart. The likely
    # transposition comes from the overall pitch-class profiles, so only a few shifts need the full search.
    cs = []
    for seg in segments:
        q = chroma(seg)
        profile_q, profile_r = q.mean(axis=0), rc.mean(axis=0)
        k = int(np.argmax([np.dot(np.roll(profile_q, s), profile_r) for s in range(BINS)]))
        cs.append(similarity(q, rc, shifts=sorted({-1, 0, 1, k - 1, k, k + 1})))
    ms = [_best_alignment(mel(s), rm) for s in segments]
    return float(np.median(cs)), float(np.median(ms))


def matches(s: tuple[float, float]) -> bool:
    return s[0] >= SAME_CHROMA and s[1] >= SAME_MEL


_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|free download|out now)\b[^\)\]]*[\)\]]",
    re.I,
)


def clean_title(raw: str) -> tuple[str, str]:
    """'Artist - Title (X Remix) | Channel [Official Video]' -> ('Artist', 'Title (X Remix)')."""
    t = re.split(r"\s+[|｜]\s+|\s+//\s+", raw)[0]
    t = _JUNK.sub("", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t).strip(" -–")
    parts = re.split(r"\s+[-–—]\s+", t, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", t.strip())


def _search(query: str, n: int) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "js_runtimes": audio._js_runtimes()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"ytsearch{n}:{query}", download=False).get("entries") or []


def _download(video: str, folder: Path) -> np.ndarray | None:
    import yt_dlp

    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{video}.flac"
    if not out.exists():
        try:
            with tempfile.TemporaryDirectory(dir=folder) as tmp:
                opts = {
                    "format": "bestaudio/best",
                    "outtmpl": f"{tmp}/a.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "max_filesize": 60_000_000,
                    "js_runtimes": audio._js_runtimes(),
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(f"https://www.youtube.com/watch?v={video}", download=True)
                src = next(p for p in Path(tmp).iterdir() if p.suffix not in (".part", ".ytdl"))
                audio._ffmpeg("-i", str(src), "-t", "900", "-ac", "1", "-ar", str(RATE), str(out))
        except Exception:
            return None
    return sf.read(str(out), dtype="float32")[0]


_BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_VERSION_WORDS = {"remix", "mix", "edit", "version", "dub", "rework", "bootleg", "flip", "vip", "extended", "radio",
                  "club", "instrumental", "acapella", "remaster", "remastered", "rmx", "refix"}  # fmt: skip
_MODIFIED = re.compile(
    r"slowed|reverb|sped[ -]?up|speed[ -]?up|nightcore|\b8d\b|karaoke|\bcover\b|1 hour|\bloop(ed)?\b|tiktok", re.I
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _segments(mix: Path, e: Entry) -> list[np.ndarray]:
    start, end = e.start + 4, e.end - 16
    times = np.arange(start, max(end, start + 0.1), 20.0)[:8]
    return [audio.read_seconds(mix, float(t), 12.0) for t in times]


_GENRES = (
    "afro house", "tech house", "deep house", "progressive house", "melodic techno", "organic house", "amapiano",
    "drum and bass", "drum & bass", "uk garage", "techno", "trance", "disco", "house", "edm", "dubstep",
)  # fmt: skip
_TIMESTAMP = re.compile(r"^\s*\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*[-–.:)]?\s*")


def listed_versions(e: Entry, description: str | None) -> list[str]:
    """Lines of the uploader's own tracklist that name this song."""
    wanted = _words(song_id(e.title))
    lines = (_TIMESTAMP.sub("", line).strip() for line in (description or "").splitlines())
    return [t for t in lines if wanted and wanted <= _words(t) and len(t) < 160][:3]


def hints_for(e: Entry, mix_title: str, description: str | None) -> list[str]:
    """Search queries from the uploader's own words: tracklist lines naming this song, and the mix's genre."""
    lines = listed_versions(e, description)
    text = f"{mix_title} {description or ''}".lower()
    genre = next((g for g in _GENRES if g in text), None)
    first_artist = re.split(r",|&| x | feat", e.artist)[0].strip()
    queries = lines[:3]
    if genre:
        queries += [f"{first_artist} {song_id(e.title)} {genre} remix", f"{song_id(e.title)} {genre} remix"]
    return queries


def _names_version(line: str, candidate: dict) -> bool:
    """The upload's title carries (nearly) every word of the uploader's tracklist line, remix credit included."""
    words = _words(line)
    return bool(words) and len(words & _words(candidate.get("title") or "")) >= 0.85 * len(words)


def find_version(
    e: Entry,
    mix: Path,
    folder: Path,
    hints: list[str] = (),
    tried: list[str] | None = None,
    listed: list[str] = (),
) -> dict | None:
    """The upload that is the version playing in this entry, if the one Shazam named isn't.

    `tried` collects the best candidates and their scores, so a decision can be checked afterwards.
    """
    tried = tried if tried is not None else []
    segments = _segments(mix, e)
    if len(segments) < 2:
        return None
    wanted = _words(song_id(e.title))
    artist = _words(e.artist) - {"feat", "ft", "x", "and", "the", "vs"}

    own_mods = _MODIFIED.findall(e.title)

    def plausible(entry: dict) -> bool:
        raw = entry.get("title") or ""
        title = _words(raw)
        altered = _MODIFIED.search(raw) and not own_mods  # a fan edit of the record, not a release
        return bool(wanted) and wanted <= title and 60 < (entry.get("duration") or 0) < 900 and not altered

    # Is it simply the version Shazam named?
    named = next(
        (c for c in _search(f"{e.artist} - {e.title}", 4) if plausible(c) and artist & _words(c["title"])), None
    )
    if named is not None and (rec := _download(named["id"], folder)) is not None:
        s = score(segments, rec)
        tried.append(f"(as named) {named.get('title')}: chroma {s[0]:.2f}, mel {s[1]:.2f}")
        if matches(s):
            return None

    seen, scored = set(), []
    first_artist = re.split(r",|&| x | feat", e.artist)[0].strip()
    queries = [*hints, f"{first_artist} {song_id(e.title)} remix", f"{song_id(e.title)} remix", f"{e.artist} {e.title}"]
    for query in queries:
        for c in _search(query, 8):
            if c["id"] in seen or not plausible(c) or len(seen) >= MAX_CANDIDATES:
                continue
            seen.add(c["id"])
            rec = _download(c["id"], folder)
            if rec is not None:
                scored.append((score(segments, rec), c))
    if not scored:
        return None
    scored.sort(key=lambda sc: sc[0][0] + sc[0][1], reverse=True)
    tried.extend(f"{c.get('title')}: chroma {s[0]:.2f}, mel {s[1]:.2f}" for s, c in scored[:5])

    # A candidate is accepted if it matches on harmony and sound, or if it is the very version the uploader's
    # tracklist names and it matches on sound (a remix can be played in another key or with its vocal swapped,
    # which upsets harmony; the uploader's naming is the corroboration).
    def accepted(s: tuple[float, float], c: dict) -> bool:
        return matches(s) or (s[1] >= LISTED_MEL and any(_names_version(line, c) for line in listed))

    good = [(s, c) for s, c in scored if accepted(s, c)]
    if not good:
        return None
    best_score, best = max(good, key=lambda sc: sc[0][1])

    # Every accepted candidate is the same recording uploaded again (two different remixes don't both pass);
    # what must be clearly behind is the best one that doesn't.
    def duplicate(s: tuple[float, float]) -> bool:  # the same recording, uploaded under another title
        return any(abs(s[0] - g[0]) <= 0.02 and abs(s[1] - g[1]) <= 0.02 for g, _ in good)

    rivals = [s for s, c in scored if not accepted(s, c) and not duplicate(s)]
    runner_up = max(rivals, key=lambda s: s[1], default=(0.0, 0.0))
    if best_score[1] - runner_up[1] < MEL_MARGIN:
        return None
    up_artist, up_title = clean_title(best.get("title") or "")
    return {
        "song": song_id(e.title),
        "start": e.start,
        "end": e.end,
        "artist": up_artist or e.artist,
        "title": up_title,
        "url": f"https://www.youtube.com/watch?v={best['id']}",
        "chroma": round(best_score[0], 3),
        "mel": round(best_score[1], 3),
    }


_LINE_TIME = re.compile(r"^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?")


def title_first(entries: list[Entry], description: str | None) -> bool:
    """Does the uploader write 'Title - Artist'? Judged from the lines that name tracks already identified."""
    votes = 0
    for _, line in listing(description):
        left, right = clean_title(line)
        for e in entries:
            if e.confirmed and not e.role.startswith("unidentified"):
                if _words(left) <= _words(e.title) and _words(right) & _words(e.artist):
                    votes += 1
                elif _words(left) & _words(e.artist) and _words(right) <= _words(e.title):
                    votes -= 1
    return votes > 0


def credit(line: str, flipped: bool) -> tuple[str, str]:
    """(artist, title) from a tracklist line, in the uploader's order."""
    left, right = clean_title(line)
    return (right, left) if flipped and left else (left, right)


def orient(entries: list[Entry], description: str | None) -> None:
    """Swap artist and title back when the uploader's tracklist credits them the other way round."""
    flipped = title_first(entries, description)
    for _, line in listing(description):
        artist, title = credit(line, flipped)
        for e in entries:
            if _words(e.artist) == _words(title) and _words(e.title) == _words(artist) and artist:
                e.artist, e.title = artist, title


def listing(description: str | None) -> list[tuple[float, str]]:
    """(seconds, 'Artist - Title') for each timestamped line of the uploader's tracklist."""
    out = []
    for line in (description or "").splitlines():
        if m := _LINE_TIME.match(line):
            parts = [int(x) for x in m.group(1).split(":")]
            seconds = sum(v * 60**i for i, v in enumerate(reversed(parts)))
            text = _TIMESTAMP.sub("", line).strip()
            text = re.sub(r"\s*\|\s*\d{4}\s*$", "", text)  # "... | 1994"
            if len(_words(text)) >= 2 and len(text) < 160:
                out.append((float(seconds), text))
    return out


def fill_from_listing(
    targets: list[Entry], named: set[str], mix: Path, folder: Path, description: str | None, flipped: bool = False
) -> list[dict]:
    """Stretches nothing confirmed covers, checked against what the uploader's tracklist says plays there.

    Each nearby tracklist line not already in the tracklist is searched for on YouTube and compared with the mix;
    only an audio match is accepted, so a wrong or shifted tracklist can't put a song in that didn't play.
    """
    lines = listing(description)
    out = []
    for e in targets:
        for t, line in lines:
            if not (e.start - 45 <= t <= e.end) or song_id(credit(line, flipped)[1]) in named:
                continue
            length = e.end - e.start
            step = 5.0 if length < 40 else 10.0 if length < 60 else 20.0
            starts = np.arange(e.start + 2, max(e.end - 12, e.start + 2.1), step)
            segments = [audio.read_seconds(mix, float(s), 12.0) for s in starts][:6]
            hit = None
            # One 12 s window is too little to vouch for a record; need two.
            for c in _search(line, 4) if len(segments) >= 2 else []:
                if not _names_version(line, c) or not 45 < (c.get("duration") or 0) < 900:
                    continue
                rec = _download(c["id"], folder)
                if rec is None:
                    continue
                s = score(segments, rec)
                if matches(s) or s[1] >= LISTED_MEL:
                    hit = (s, c)
                    break
            if not hit:
                out.append({"song": None, "start": e.start, "end": e.end, "hint": line})
                continue
            artist, title = credit(line, flipped)
            out.append({
                "song": song_id(title), "start": max(e.start, t if t > e.start else e.start), "end": e.end,
                "artist": artist, "title": title, "url": f"https://www.youtube.com/watch?v={hit[1]['id']}",
                "chroma": round(hit[0][0], 3), "mel": round(hit[0][1], 3), "added": True,
            })  # fmt: skip
            named.add(song_id(title))
            break
    return out


def apply(entries: list[Entry], versions: list[dict]) -> None:
    """Rename entries to the versions found, and retire the stand-in matches that version explains."""
    for v in [v for v in versions if v.get("hint")]:
        e = next(
            (x for x in entries if (x.role.startswith("unidentified") or not x.confirmed)
             and min(x.end, v["end"]) - max(x.start, v["start"]) > 0 and x.listed_as is None),
            None,
        )  # fmt: skip
        if e is not None and e.role.startswith("unidentified"):
            e.listed_as = v["hint"]
    for v in [v for v in versions if v.get("added")]:
        # Confirmed from the uploader's tracklist: promote a stray match of it, or take over an "ID" stretch.
        twin = next(
            (x for x in entries if not x.confirmed and song_id(x.title) == v["song"]
             and min(x.end, v["end"]) - max(x.start, v["start"]) > 0),
            None,
        )  # fmt: skip
        if twin is None:
            twin = Entry(start=v["start"], end=v["end"], artist=v["artist"], title=v["title"], role="track")
            entries.append(twin)
        twin.artist, twin.title, twin.url = v["artist"], v["title"], v["url"]
        twin.confirmed, twin.version_from = True, "listing"
        if re.search(r"a ?cap+el+a|\bvox\b|vocal only", v["title"], re.I):
            twin.role = "acapella"
        for o in list(entries):
            overlap = min(o.end, twin.end) - max(o.start, twin.start)
            if o.role.startswith("unidentified") and overlap > 0.5 * (o.end - o.start):
                entries.remove(o)
    for v in [v for v in versions if not v.get("added") and not v.get("hint")]:
        e = next(
            (
                x
                for x in entries
                if x.confirmed and song_id(x.title) == v["song"] and min(x.end, v["end"]) - max(x.start, v["start"]) > 0
            ),
            None,
        )
        if e is None:
            continue
        # Rename only when the upload names a version (a remix, edit, extended mix...) Shazam's title lacks;
        # otherwise it's the same record under a compilation's or uploader's title.
        added = _words(" ".join(_BRACKETED.findall(v["title"]))) - _words(e.title)
        renamed = bool(added & _VERSION_WORDS) or bool(v.get("added"))
        e.url, e.uncertain = v["url"], False
        if renamed:
            e.artist, e.title = v["artist"], v["title"]
        e.version_from = "youtube" if renamed else None
        for o in entries:
            overlap = min(o.end, e.end) - max(o.start, e.start)
            if o is not e and o.confirmed and o.uncertain and overlap > 0.5 * (o.end - o.start):
                o.confirmed = False


def load(path: Path) -> dict:
    data = json.loads(path.read_text()) if path.exists() else {}
    return {"checked": data.get("checked", []), "found": data.get("found", []), "tried": data.get("tried", {})}


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1))
