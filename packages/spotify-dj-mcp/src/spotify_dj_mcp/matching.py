"""Match Spotify tracks to tracks in a local DJ library by title and artist.

A remix is a different track from the original, but "Extended Mix", "Original Mix", "Radio Edit",
"[Explicit]", "Remastered" etc. are treated as the same song (DJ libraries usually hold extended versions
of what streaming services list as radio edits).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

GENERIC_VERSION = re.compile(
    r"\b(original|extended|radio|club|album|single|full length|clean|dirty|explicit|remaster(ed)?|"
    r"edit|mix|version|mono|stereo|feat\.?|ft\.?|featuring|\d{4})\b"
)
FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring)\s+[^\)\]]*[\)\]]?", re.I)
CREDIT = re.compile(r"(feat\.?|ft\.?|featuring|with)\s", re.I)
SPLIT_ARTISTS = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|/|;)\s*", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s).strip()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def split_title(title: str) -> tuple[str, str]:
    """('Song (Artist Remix) [Explicit]') -> ('song', 'artist remix'). Version words that don't change the
    song (extended, radio edit, explicit, ...) are dropped from the version part."""
    t = _fold(title)
    t = re.sub(r"^\d{1,3}\s*[-.]\s+", "", t)  # "01 - Song" / "01. Song" from file names
    # Bracketed credits like "(feat. X)" or "(with X)" name artists, not a version of the song.
    versions = [v for v in re.findall(r"[\(\[]([^\)\]]*)[\)\]]", t) if not CREDIT.match(v.strip())]
    base = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t)
    if " - " in base:
        base, rest = base.split(" - ", 1)
        versions.append(rest)
    version = " ".join(_clean(GENERIC_VERSION.sub(" ", v)) for v in versions)
    return _clean(FEAT.sub(" ", base)), _clean(version)


def artist_set(artists: list[str] | str) -> set[str]:
    if isinstance(artists, str):
        artists = [artists]
    out: set[str] = set()
    for a in artists:
        for part in SPLIT_ARTISTS.split(_fold(a)):
            part = _clean(part)
            if part:
                out.add(part)
    return out


@dataclass
class LibraryTrack:
    id: str
    title: str
    artist: str
    base: str = ""
    version: str = ""
    artists: frozenset = frozenset()

    def __post_init__(self):
        self.base, self.version = split_title(self.title)
        self.artists = frozenset(artist_set(self.artist))


@dataclass
class Match:
    track: LibraryTrack
    confidence: str  # "exact" | "likely" | "different version"
    score: float


def _artists_overlap(a: frozenset, b: set[str], title: str = "") -> float:
    """Share of wanted artists `b` found among library artists `a`, or credited in the library title
    (remixers are often only in the title, e.g. "Song (Remixer Remix)")."""
    if not b or not (a or title):
        return 0.0
    words = f" {title} "
    hits = sum(1 for x in b if any(x == y or (len(x) > 3 and (x in y or y in x)) for y in a) or f" {x} " in words)
    return hits / len(b)


class Matcher:
    def __init__(self, library: list[dict]):
        """library: dicts with id, title, artist (as returned by rekordbox search_tracks)."""
        self.tracks = [LibraryTrack(str(t["id"]), t.get("title") or "", t.get("artist") or "") for t in library]
        self.by_base: dict[str, list[LibraryTrack]] = {}
        for t in self.tracks:
            if t.base:
                self.by_base.setdefault(t.base, []).append(t)
        self._bases = list(self.by_base)

    def match(self, name: str, artists: list[str]) -> Match | None:
        base, version = split_title(name)
        if not base:
            return None
        wanted_artists = artist_set(artists)
        candidates = list(self.by_base.get(base, []))
        if not candidates:
            for b in difflib.get_close_matches(base, self._bases, n=3, cutoff=0.88):
                candidates += self.by_base[b]
        best: Match | None = None
        for c in candidates:
            title_score = difflib.SequenceMatcher(None, base, c.base).ratio()
            overlap = _artists_overlap(c.artists, wanted_artists, _clean(_fold(c.title)))
            if overlap == 0 or (not c.artists and overlap < 0.5):
                continue
            score = 0.6 * title_score + 0.4 * min(1.0, overlap)
            same_version = version == c.version
            if not same_version and difflib.SequenceMatcher(None, version, c.version).ratio() > 0.8:
                same_version = True
            if same_version:
                confidence = "exact" if title_score == 1 and overlap >= 0.5 else "likely"
            else:
                confidence = "different version"
                score -= 0.3
            if best is None or score > best.score:
                best = Match(c, confidence, round(score, 3))
        return best
