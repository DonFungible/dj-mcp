"""Turning per-clip matches into a tracklist where tracks may overlap.

Each probe is a clip of one source (the full mix, the vocal stem or the instrumental stem) at one time. A song
heard in several probes close together is one "play"; plays can overlap (a blend, or an acapella over another
track). The role of a play comes from which sources heard it: only the vocal stem means an acapella, only the
instrumental stem means it was the bed under someone else's vocal.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import pairwise

from .recognize import Match
from .verify import judge

MIX, VOCALS, INSTRUMENTAL = "mix", "vocals", "instrumental"
MAX_INTRO = 48.0  # longest stretch (about 24 bars) a record is assumed to play unheard under the previous one


@dataclass(frozen=True)
class Probe:
    t: float
    source: str
    match: Match | None


@dataclass
class Entry:
    start: float
    end: float
    artist: str
    title: str
    role: str  # track | acapella | instrumental | unidentified | unidentified vocal
    hits: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    url: str | None = None
    lyrics: str | None = None
    n: int = 0
    layered_with: list[int] = field(default_factory=list)
    mixed_in_from: float | None = None  # started this early under the previous record, before it took over
    uncertain: bool = False  # matches disagree about where in the record we are: maybe a version Shazam lacks
    confirmed: bool = True  # several matches agree on when the record started; otherwise likely a stray loop
    version_from: str | None = None  # "youtube" when the exact version was found by comparing uploads
    listed_as: str | None = None  # what the uploader's tracklist says plays here, when the audio couldn't confirm it

    @property
    def audible_from(self) -> float:
        return self.start if self.mixed_in_from is None else self.mixed_in_from

    @property
    def confidence(self) -> str:
        if self.role.startswith("unidentified"):
            return "none"
        if not self.confirmed:
            return "low"
        return "medium" if self.uncertain else "high"


_BRACKETS = re.compile(r"[\(\[].*?[\)\]]")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b\.?.*$", re.I)
_VERSION = re.compile(r"\s[-–]\s.*\b(remix|mix|edit|version|rework|dub|bootleg|flip|vip)\b.*$", re.I)
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def song_id(title: str) -> str:
    """Remixes, edits and 'feat.' credits of one song share this, so they count as the same song."""
    t = _VERSION.sub("", title)
    t = _BRACKETS.sub(" ", t)
    t = _FEAT.sub("", t)
    t = _NON_WORD.sub(" ", t.lower().replace("&", " and ")).strip()
    return t or title.lower().strip()


def continuous(ta: float, ma: Match | None, tb: float, mb: Match | None) -> bool:
    """The same record playing straight through from ta to tb (or nothing recognisable at either end).

    Shazam's offset is the position in the original record; if it advanced by about the time between the probes
    (allowing for the DJ's tempo change), nothing happened in between worth probing for.
    """
    if ma is None or mb is None:
        return ma is None and mb is None
    if song_id(ma.title) != song_id(mb.title):
        return False
    dt = tb - ta
    return abs((mb.offset - ma.offset) - dt) <= 0.1 * dt + 4


def build(
    probes: list[Probe],
    duration: float,
    window: float,
    *,
    hop: float = 20.0,
    max_gap: float = 100.0,
    min_gap_report: float = 25.0,
    beds: list[dict] | None = None,
) -> list[Entry]:
    """Plays of each song, plus unidentified stretches, ordered by start time and numbered."""
    probes = _unswap_credits(probes)
    times_by_source: dict[str, list[float]] = {}
    for p in probes:
        times_by_source.setdefault(p.source, []).append(p.t)
    for ts in times_by_source.values():
        ts.sort()

    hits: dict[str, list[Probe]] = {}
    for p in probes:
        if p.match:
            hits.setdefault(song_id(p.match.title), []).append(p)

    plays: list[list[Probe]] = []
    for song_hits in hits.values():
        song_hits.sort(key=lambda p: p.t)
        plays.append([song_hits[0]])
        for p in song_hits[1:]:
            if p.t - plays[-1][-1].t > max_gap:
                plays.append([])
            plays[-1].append(p)

    plays = [p for p in plays if not _sandwiched(p, plays)]
    instrumental = [p for p in probes if p.source == INSTRUMENTAL]
    # One credit per song across the whole set: a key-shifted acapella can match some other upload of the song,
    # and the credit heard most often is the likelier one.
    credits: dict[str, Counter] = {}
    for p in probes:
        if p.match:
            credits.setdefault(song_id(p.match.title), Counter())[(p.match.artist, p.match.title)] += 1
    made = [_entry(p, times_by_source, duration, window, hop, instrumental, beds or [], credits) for p in plays]
    entries = [e for e, _ in made]
    # An acapella "over" a named record needs that record to have really played (confirmed), not just a few
    # instrumental-stem misfires; otherwise it's the vocal's own record after all, if the mix heard it.
    for e, bed in made:
        if bed is None:
            continue
        under = [
            o
            for o in entries
            if o.confirmed and song_id(o.title) == bed and min(o.end, e.end) - max(o.start, e.start) > 0
        ]
        if not under and e.sources.get(MIX):
            e.role = "track"
    entries = _merge_contained_echoes(entries)
    entries += _gaps(entries, duration, min_gap_report)
    return number(entries)


def _unswap_credits(probes: list[Probe]) -> list[Probe]:
    """Shazam sometimes has one release with artist and title swapped; bring those onto one credit."""
    pairs = Counter((song_id(p.match.title), song_id(p.match.artist)) for p in probes if p.match)
    out = []
    for p in probes:
        if p.match:
            t, a = song_id(p.match.title), song_id(p.match.artist)
            flipped, here = pairs.get((a, t), 0), pairs[(t, a)]
            if a != t and flipped and (flipped > here or (flipped == here and a < t)):
                p = Probe(p.t, p.source, replace(p.match, artist=p.match.title, title=p.match.artist))
        out.append(p)
    return out


def _sandwiched(play: list[Probe], plays: list[list[Probe]]) -> bool:
    """A one-off match between two hits of another play on the same source: a misfire during that play.

    A one-off heard only on a stem always counts: stems are probed densely where they matter (around vocals),
    so a real acapella or bed is heard more than once, and a lone stem match is more likely a misfire.
    """
    if len({p.t for p in play}) > 1:
        return False
    if play[0].source != MIX:
        return True
    t, source = play[0].t, play[0].source
    for other in plays:
        if other is play:
            continue
        times = [p.t for p in other if p.source == source]
        if any(x < t for x in times) and any(x > t for x in times):
            return True
    return False


def _entry(
    play: list[Probe],
    times: dict[str, list[float]],
    duration: float,
    window: float,
    hop: float,
    instrumental: list[Probe] = (),
    beds: list[dict] | None = None,
    credits: dict[str, Counter] | None = None,
) -> tuple[Entry, str | None]:
    detected = {p.t for p in play}
    first, last = play[0], play[-1]

    # It takes over somewhere between the last probe that missed it and the first that heard it. The match
    # offset says how far into the record that first hit was: DJs usually start a record near its top and blend
    # it in under the outgoing one, so a modest offset dates when it really started (mixed_in_from). A stem isn't
    # masked by the other record, and an acapella's offset says nothing about when the DJ dropped it in.
    # When the full mix hears the record, that's when it plays; vocal-stem hits before that are the DJ teasing
    # its vocal over the previous record, which counts as mixing it in, not as the start.
    anchor = next((p for p in play if p.source == MIX), first)
    prev = max((t for t in times.get(anchor.source, []) if t < anchor.t and t not in detected), default=None)
    mixed_in_from = None
    if anchor.source == MIX:
        by_offset = anchor.t - anchor.match.offset
        start = (prev + anchor.t) / 2 if prev is not None else max(0.0, by_offset)
        if anchor.match.offset <= MAX_INTRO:
            start = max(start, by_offset)
            if by_offset < start - 5:
                mixed_in_from = max(0.0, by_offset)
        if first is not anchor and first.t < start - 5:
            mixed_in_from = min(mixed_in_from if mixed_in_from is not None else start, first.t)
    else:
        # Vocal probes only happen where someone is singing, so the previous one can be far back.
        start = first.t - hop / 2 if prev is None else max(first.t - hop / 2, (prev + first.t) / 2)

    nxt = min((t for t in times.get(last.source, []) if t > last.t and t not in detected), default=None)
    if last.source == MIX:
        end = (last.t + nxt) / 2 + window / 2 if nxt is not None else duration
    else:
        end = last.t + window if nxt is None else min(last.t + window, (last.t + nxt) / 2 + window / 2)
    end = min(end, duration)

    counts = Counter(p.source for p in play)
    # What the instrumental stem heard while this song was on: another record, more than once, and never this
    # song's own instrumental, means this is an acapella over that record, even if the vocal was loud enough to
    # dominate the full mix as well.
    own = song_id(first.match.title)
    bed_hits = Counter(
        song_id(p.match.title) for p in instrumental if first.t - 1 <= p.t <= last.t + 1 and p.match is not None
    )
    # And where Shazam couldn't name that music, how it compared with the vocal's own released record.
    scores = [
        r["score"] for r in beds or [] if song_id(r["title"]) == own and first.t - window <= r["t"] <= last.t + window
    ]
    # Heard twice under the vocal, or once if it's at least half of what the instrumental stem named there (short
    # acapellas get few probes); build() then checks that record really played at that time.
    top = max(bed_hits.values(), default=0)
    named_other_bed = top >= 2 or (top >= 1 and top * 2 >= sum(bed_hits.values()))
    sounds_like_other_record = judge(scores) == "other"
    if counts[VOCALS] and not bed_hits[own] and (named_other_bed or sounds_like_other_record):
        role = "acapella"
    elif counts[MIX] or (counts[VOCALS] and counts[INSTRUMENTAL]):
        role = "track"
    elif counts[VOCALS]:
        role = "acapella"
    else:
        role = "instrumental"

    # A record playing straight through moves forward in Shazam's offsets as fast as the clock. When most
    # neighbouring matches of the same version don't, Shazam is matching pieces of something it doesn't know.
    mix_hits = [p for p in play if p.source == MIX]
    pairs = [(a, b) for a, b in pairwise(mix_hits) if a.match.key == b.match.key]
    steady = sum(continuous(a.t, a.match, b.t, b.match) for a, b in pairs)
    uncertain = len(pairs) >= 4 and steady / len(pairs) < 0.4
    confirmed = _aligned(play, role)

    # Most common credit wins; on a tie prefer the longer title, which tends to carry the remix name.
    names = (credits or {}).get(own) or Counter((p.match.artist, p.match.title) for p in play)
    (artist, title), _ = max(names.items(), key=lambda kv: (kv[1], len(kv[0][1])))
    url = next((p.match.url for p in play if (p.match.artist, p.match.title) == (artist, title) and p.match.url), None)
    entry = Entry(
        start=max(0.0, start),
        end=max(end, start + 1),
        artist=artist,
        title=title,
        role=role,
        hits=len({p.t for p in play}),
        sources=dict(counts),
        url=url,
        mixed_in_from=mixed_in_from,
        uncertain=uncertain,
        confirmed=confirmed,
    )
    # The record an acapella was judged to be over by name only; build() checks that record really played.
    named_bed = None
    if role == "acapella" and named_other_bed and not sounds_like_other_record:
        named_bed = bed_hits.most_common(1)[0][0]
    return entry, named_bed


def _aligned(play: list[Probe], role: str) -> bool:
    """Do enough matches agree on when the record started (clock time minus position in the record)?

    A record really playing gives matches whose offsets advance with the clock, so they agree on its start: three
    probe times, or two at least 20 s apart. Shazam matching a loop or sample shared with another record gives
    offsets that stay put or jump. Two guards on top of that:

    - a loop matched over and over can line up within one pass of the loop, so across the whole play the offsets
      must cover at least half as much of the record as the clock covered;
    - the instrumental stem misfires a lot (house instrumentals are mostly loops), so apart from a bed under an
      acapella, at least two of the agreeing probe times must come from the full mix or the vocal stem.
    """
    keys = Counter(p.match.key for p in play)
    main = [p for p in play if p.match.key == keys.most_common(1)[0][0]]
    clock = max(p.t for p in main) - min(p.t for p in main)
    covered = max(p.match.offset for p in main) - min(p.match.offset for p in main)
    if clock >= 30 and covered < 0.5 * clock:
        return False

    points = [(p.t, p.t - p.match.offset, p.match.key, p.source) for p in play]
    for t0, c0, k0, _ in points:
        group = [(t, src) for t, c, k, src in points if k == k0 and abs(c - c0) <= 6 + 0.05 * abs(t - t0)]
        times = {t for t, _ in group}
        if not (len(times) >= 3 or (len(times) >= 2 and max(times) - min(times) >= 20)):
            continue
        if role != "instrumental" and len({t for t, src in group if src != INSTRUMENTAL}) < 2:
            continue
        return True
    return False


def _merge_contained_echoes(entries: list[Entry]) -> list[Entry]:
    """Drop one-hit sightings that sit inside a longer play of the same song (a stem echoing the main hit)."""
    keep = []
    for e in entries:
        twin = next(
            (
                o
                for o in entries
                if o is not e
                and song_id(o.title) == song_id(e.title)
                and o.hits > e.hits
                and o.start - 5 <= e.start
                and e.end <= o.end + 5
            ),
            None,
        )
        if twin is None or e.hits > 1:
            keep.append(e)
    return keep


def _gaps(entries: list[Entry], duration: float, min_len: float) -> list[Entry]:
    """Stretches nothing was identified in, so the list shows there's a track it missed."""
    covered = sorted((e.audible_from, e.end) for e in entries if e.role != "unidentified vocal" and e.confirmed)
    gaps, cursor = [], 0.0
    for s, e in covered:
        if s - cursor >= min_len:
            gaps.append(Entry(start=cursor, end=s, artist="ID", title="ID", role="unidentified"))
        cursor = max(cursor, e)
    if duration - cursor >= min_len:
        gaps.append(Entry(start=cursor, end=duration, artist="ID", title="ID", role="unidentified"))
    return gaps


def number(entries: list[Entry]) -> list[Entry]:
    """Number the tracklist in time order, then the unconfirmed matches after it."""
    listed = sorted((e for e in entries if e.confirmed), key=lambda e: (e.start, e.end))
    extra = sorted((e for e in entries if not e.confirmed), key=lambda e: (e.start, e.end))
    # A record started under nothing that was really playing (only stray loop matches) starts when it started.
    for e in listed:
        if e.mixed_in_from is not None and not any(
            o is not e and o.start < e.start and o.end > e.mixed_in_from + 5 and o.role != "unidentified"
            for o in listed
        ):
            e.start, e.mixed_in_from = e.mixed_in_from, None
    listed.sort(key=lambda e: (e.start, e.end))
    entries[:] = listed + extra
    for i, e in enumerate(entries, 1):
        e.n = i
    for e in entries:
        e.layered_with = [
            o.n
            for o in listed
            if o is not e and min(e.end, o.end) - max(e.audible_from, o.audible_from) >= 8 and o.role != "unidentified"
        ]
    return entries


def fmt_time(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render(entries: list[Entry], title: str | None = None, duration: float | None = None) -> str:
    lines = []
    if title:
        lines.append(f"{title}" + (f"  ({fmt_time(duration)})" if duration else ""))
        lines.append("")
    by_n = {e.n: e for e in entries}
    for e in entries:
        if not e.confirmed and e is next(x for x in entries if not x.confirmed):
            lines.append("")
            lines.append(
                "Possible extra layers (matched, but the matches don't line up the way a record playing does;"
                " often a shared loop or sample, sometimes a short layer):"
            )
        name = "ID - ID (not identified)" if e.role == "unidentified" else f"{e.artist} - {e.title}"
        if e.role == "unidentified vocal":
            name = "Unidentified vocal"
        tag = {"acapella": "  [acapella]", "instrumental": "  [instrumental]"}.get(e.role, "")
        note = ""
        if e.role in ("acapella", "unidentified vocal", "instrumental"):
            under = [
                by_n[n]
                for n in e.layered_with
                if by_n[n].role in ("track", "instrumental")
                and min(e.end, by_n[n].end) - max(e.start, by_n[n].start) >= 8
            ]
            if under:
                word = "under" if e.role == "instrumental" else "over"
                note = f"  {word} " + " + ".join(f"#{o.n} {o.title}" for o in under)
            elif e.role == "acapella":
                note = "  over music Shazam couldn't name"
        elif e.role == "track":
            blends = [
                by_n[n]
                for n in e.layered_with
                if by_n[n].role == "track" and by_n[n].start < e.start and by_n[n].end > e.audible_from
            ]
            if blends:
                o = blends[-1]
                if e.mixed_in_from is not None:
                    note = f"  (mixed in over #{o.n} from {fmt_time(e.mixed_in_from)})"
                else:
                    note = f"  (blends with #{o.n} until {fmt_time(min(o.end, e.end))})"
        unsure = ""
        if e.uncertain:
            unsure += "  (uncertain: maybe a remix or edit Shazam doesn't know)"
        if e.version_from == "youtube":
            unsure += "  (version found on YouTube)"
        elif e.version_from == "listing":
            unsure += "  (from the uploader's tracklist, checked against the audio)"
        lines.append(f"{e.n:>3}. {fmt_time(e.start):>7}–{fmt_time(e.end):<7}  {name}{tag}{note}{unsure}")
        if e.lyrics:
            lines.append(f"{'':>21}lyrics: “{e.lyrics}”")
        if e.listed_as:
            lines.append(f"{'':>21}uploader's tracklist says: {e.listed_as} (not confirmed by the audio)")
    found = [e for e in entries if e.confirmed and not e.role.startswith("unidentified")]
    extra = sum(not e.confirmed for e in entries)
    lines.append("")
    lines.append(
        f"{len(found)} identified ({sum(e.role == 'acapella' for e in found)} acapellas), "
        f"{sum(e.role.startswith('unidentified') for e in entries)} unidentified"
        + (f", {extra} possible extra layers." if extra else ".")
    )
    return "\n".join(lines)
