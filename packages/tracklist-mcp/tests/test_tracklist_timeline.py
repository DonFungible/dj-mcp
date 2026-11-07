from tracklist_mcp.recognize import Match
from tracklist_mcp.timeline import INSTRUMENTAL, MIX, VOCALS, Probe, build, continuous, render, song_id

W = 12.0


def m(title: str, offset: float = 60.0, artist: str = "Artist") -> Match:
    return Match(key=title, title=title, artist=artist, offset=offset, timeskew=0.0)


def hit(t: float, source: str, title: str, start: float = 0.0, artist: str = "Artist") -> Probe:
    """A probe at `t` hearing `title`, a record that started playing at `start` (so its offset is t - start)."""
    return Probe(t, source, m(title, offset=t - start, artist=artist))


def test_song_id_merges_versions():
    assert song_id("Move (feat. Adam Port, Keinemusik & Orso)") == song_id("Move")
    assert song_id("Stereo Love - Padé Afro House Remix") == song_id("Stereo Love")
    assert song_id("She Doesn't Mind [Extended Mix]") == song_id("She Doesn't Mind")
    assert song_id("Lady (Hear Me Tonight)") != song_id("Lady Marmalade")


def test_sequence_with_blend():
    # A plays 0-100, B comes in at 80 (blend at 80-100), then B alone.
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60, 80)]
    probes += [hit(t, MIX, "B", start=75) for t in (100, 120, 140, 160)]
    entries = build(probes, duration=180, window=W)
    assert [e.title for e in entries] == ["A", "B"]
    a, b = entries
    assert a.start == 0
    assert b.start == 90  # takes over between the probe at 80 (still A) and 100
    assert b.mixed_in_from == 75  # but its offset says it was started under A at 75
    assert a.role == b.role == "track"
    assert b.n in a.layered_with


def test_start_uses_match_offset():
    # B first heard at 100 but its offset says it began at 92.
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60, 80)]
    probes += [hit(t, MIX, "B", start=92) for t in (100, 120)]
    b = next(e for e in build(probes, 140, W) if e.title == "B")
    assert b.start == 92


def test_acapella_over_other_track():
    probes = [hit(t, MIX, "Beat") for t in range(0, 200, 20)]
    probes += [hit(t, INSTRUMENTAL, "Beat") for t in (60, 80, 100)]
    probes += [hit(t, VOCALS, "Vox", start=50) for t in (60, 80, 100)]
    probes += [Probe(t, VOCALS, None) for t in (40, 120)]
    entries = build(probes, 210, W)
    vox = next(e for e in entries if e.title == "Vox")
    beat = next(e for e in entries if e.title == "Beat")
    assert vox.role == "acapella"
    assert beat.role == "track"
    assert 40 < vox.start <= 60 and 100 < vox.end < 140
    assert beat.n in vox.layered_with
    text = render(entries)
    assert "[acapella]" in text and f"over #{beat.n} Beat" in text


def test_replayed_song_is_two_plays():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40)]
    probes += [hit(t, MIX, "B", start=50) for t in range(60, 400, 20)]
    probes += [hit(t, MIX, "A", start=380) for t in (400, 420)]
    titles = [e.title for e in build(probes, 440, W) if e.role == "track"]
    assert titles == ["A", "B", "A"]


def test_gap_is_reported_as_unidentified():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40)]
    probes += [Probe(t, MIX, None) for t in (60, 80, 100, 120)]
    probes += [hit(t, MIX, "B", start=140) for t in (140, 160, 180)]
    entries = build(probes, 200, W)
    assert [e.role for e in entries] == ["track", "unidentified", "track"]
    gap = entries[1]
    assert 55 <= gap.start <= 70 and 125 <= gap.end <= 140


def test_single_stem_echo_inside_main_play_is_dropped():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60)]
    probes += [Probe(40, VOCALS, m("A (Acapella)", offset=40))]
    entries = build(probes, 80, W)
    assert len(entries) == 1 and entries[0].role == "track"
    assert entries[0].sources == {MIX: 4, VOCALS: 1}


def test_one_off_misfire_inside_another_play_is_dropped():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60)]
    probes += [Probe(80, MIX, m("Blip")), hit(100, MIX, "A")]
    assert [e.title for e in build(probes, 120, W)] == ["A"]


def test_one_off_between_two_songs_is_listed_apart():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60)]
    probes += [Probe(80, MIX, m("Short", offset=10))]
    probes += [hit(t, MIX, "B", start=95) for t in (100, 120, 140)]
    entries = build(probes, 160, W)
    assert [e.title for e in entries if e.confirmed] == ["A", "B"]
    text = render(entries)
    assert text.index("Possible extra layers") < text.index("Short")


def test_loop_matches_that_never_line_up_are_not_in_the_tracklist():
    # Deep house: Shazam keeps matching a shared loop in another record. Its offsets stay put instead of
    # advancing with the clock, so they never agree on when that record started.
    probes = [hit(t, MIX, "Real", start=0) for t in (0, 60, 120, 180, 240)]
    probes += [Probe(t, MIX, m("Loop", offset=o)) for t, o in [(7.5, 91), (22.5, 92), (52.5, 92), (90, 86)]]
    entries = build(probes, 260, W)
    real = next(e for e in entries if e.title == "Real")
    loop = next(e for e in entries if e.title == "Loop")
    assert real.confirmed and not loop.confirmed
    assert loop.n > real.n and loop.n not in real.layered_with
    assert loop.confidence == "low"
    text = render(entries)
    assert "1 identified" in text and "1 possible extra layers" in text


def test_continuous():
    assert continuous(0, m("A", 30), 60, m("A", 90))
    assert continuous(0, m("A", 30), 60, m("A", 93))  # played 5% faster
    assert not continuous(0, m("A", 30), 60, m("A", 40))  # re-cued or looped
    assert not continuous(0, m("A", 30), 60, m("B", 90))
    assert not continuous(0, m("A", 30), 60, None)
    assert continuous(0, None, 60, None)


def test_loud_acapella_that_dominates_the_mix():
    # The vocal of "Vox" is louder than the record under it, so the full mix hears Vox too;
    # only the instrumental stem reveals "Bed" underneath.
    probes = [hit(t, MIX, "Intro") for t in (0, 20, 40)]
    probes += [hit(t, MIX, "Vox", start=50) for t in (60, 80, 100)]
    probes += [hit(t, VOCALS, "Vox", start=50) for t in (60, 80, 100)]
    probes += [hit(t, INSTRUMENTAL, "Bed", start=30) for t in (60, 80, 100)]
    probes += [hit(t, MIX, "Outro", start=110) for t in (120, 140, 160)]
    entries = {e.title: e for e in build(probes, 180, W)}
    assert entries["Vox"].role == "acapella"
    assert entries["Bed"].role == "instrumental"
    text = render(list(entries.values()))
    assert f"[acapella]  over #{entries['Bed'].n} Bed" in text


def test_vocal_track_with_its_own_instrumental_is_a_track():
    probes = [hit(t, MIX, "Song") for t in (0, 20, 40, 60)]
    probes += [hit(t, VOCALS, "Song") for t in (20, 40)]
    probes += [hit(t, INSTRUMENTAL, "Song") for t in (20, 40)]
    [e] = build(probes, 80, W)
    assert e.role == "track"


def test_acapella_over_music_shazam_cannot_name():
    # The instrumental stem under "Vox" isn't recognised, but it doesn't match Vox's own record either.
    probes = [hit(t, MIX, "Vox") for t in (0, 20, 40, 60)]
    probes += [hit(t, VOCALS, "Vox") for t in (0, 20, 40, 60)]
    probes += [Probe(t, INSTRUMENTAL, None) for t in (0, 20, 40, 60)]

    def beds(*scores):
        return [{"t": float(i * 6), "key": "Vox", "title": "Vox", "score": sc} for i, sc in enumerate(scores)]

    [e] = build(probes, 80, W, beds=beds(0.3, 0.35, 0.6, 0.4))
    assert e.role == "acapella"
    assert "over music Shazam couldn't name" in render([e])

    assert build(probes, 80, W, beds=beds(0.97, 0.95, 0.4, 0.96))[0].role == "track"
    assert build(probes, 80, W, beds=beds(0.3, 0.4))[0].role == "track"  # too few windows to judge
    assert build(probes, 80, W, beds=beds(0.6, 0.65, 0.7))[0].role == "track"  # inconclusive
    assert build(probes, 80, W)[0].role == "track"  # no evidence either way


def test_uncertain_when_offsets_jump_around():
    # X lines up for a while (80-120 s) but mostly jumps around: a version Shazam doesn't know.
    jumpy = [Probe(t, MIX, m("X", offset=o)) for t, o in [(0, 100), (20, 30), (40, 170), (60, 5), (80, 120)]]
    jumpy += [Probe(t, MIX, m("X", offset=t + 40)) for t in (100, 120)]
    steady = [hit(t, MIX, "Y", start=190) for t in (200, 220, 240, 260, 280, 300)]
    entries = {e.title: e for e in build(jumpy + steady, 320, W)}
    assert entries["X"].uncertain and not entries["Y"].uncertain
    assert entries["X"].confirmed
    assert "uncertain" in render(list(entries.values()))


def test_one_off_stem_match_is_dropped():
    probes = [hit(t, MIX, "A") for t in (0, 20, 40, 60)]
    probes += [hit(t, VOCALS, "A") for t in (0, 20)]
    probes += [Probe(40, VOCALS, m("Misfire")), hit(60, VOCALS, "B", start=40), hit(67.5, VOCALS, "B", start=40)]
    titles = {e.title for e in build(probes, 80, W)}
    assert "Misfire" not in titles and "B" in titles


def test_one_credit_per_song_across_the_set():
    probes = [hit(t, MIX, "I Adore You", artist="HUGEL") for t in (0, 20, 40, 60)]
    probes += [hit(t, MIX, "Other", start=190) for t in (200, 220, 240)]
    probes += [hit(t, VOCALS, "I Adore You", start=180, artist="Some Cover") for t in (200, 220)]
    acapella = next(e for e in build(probes, 260, W) if e.start >= 190 and e.title == "I Adore You")
    assert acapella.artist == "HUGEL"


def test_record_started_under_only_loop_matches_starts_when_it_started():
    probes = [Probe(t, MIX, m("Loop", offset=o)) for t, o in [(0, 91), (15, 92), (30, 92)]]
    probes += [hit(t, MIX, "Opener", start=2) for t in (45, 60, 120)]
    opener = next(e for e in build(probes, 140, W) if e.title == "Opener")
    assert opener.start == 2 and opener.mixed_in_from is None


def test_loop_matched_on_mix_and_vocals_is_still_a_loop():
    # From a real deep house set: one ~15 s loop matched again and again on both the mix and the vocal stem.
    # Within one pass the matches agree; across 105 s they cover only ~19 s of the record.
    loop = [(0, VOCALS, 83), (7.5, MIX, 91), (15, VOCALS, 99), (22.5, MIX, 92), (30, MIX, 99), (45, VOCALS, 85)]
    loop += [(52.5, MIX, 92), (60, MIX, 100), (90, MIX, 86), (105, MIX, 102)]
    probes = [Probe(t, src, m("Loop", offset=o)) for t, src, o in loop]
    probes += [hit(t, MIX, "Real", start=1) for t in (45, 82.5, 120)]
    entries = {e.title: e for e in build(probes, 140, W)}
    assert entries["Real"].confirmed and not entries["Loop"].confirmed


def test_instrumental_misfires_dont_make_a_track_or_an_acapella():
    # A vocal track; the instrumental stem under it misfires twice on another record that never really plays.
    probes = [hit(t, MIX, "Starlight", start=1226) for t in (1237.5, 1260, 1290, 1320, 1350, 1380)]
    probes += [hit(t, VOCALS, "Starlight", start=1226) for t in (1245, 1305)]
    probes += [hit(t, INSTRUMENTAL, "Morning", start=1237) for t in (1350, 1380)]
    probes += [hit(1350, MIX, "Morning", start=1237)]
    entries = {e.title: e for e in build(probes, 1400, W)}
    assert entries["Starlight"].role == "track" and entries["Starlight"].confirmed
    assert not entries["Morning"].confirmed


def test_swapped_artist_and_title_are_one_song():
    probes = [
        Probe(t, MIX, Match(key="a", title="Close To You", artist="Fun Factory", offset=t - 700, timeskew=0))
        for t in (765, 780, 795)
    ]
    probes += [
        Probe(t, MIX, Match(key="b", title="Fun factory", artist="Close To You", offset=t - 700, timeskew=0))
        for t in (772.5, 787.5, 802.5)
    ]
    tracks = [e for e in build(probes, 830, W) if e.role == "track"]
    assert len(tracks) == 1 and tracks[0].title == "Close To You"


def test_teased_vocal_before_the_record_plays_is_mixed_in_not_the_start():
    probes = [hit(t, MIX, "A") for t in (0, 30, 60, 90, 110)]
    probes += [hit(t, VOCALS, "B", start=-20) for t in (20, 40)]  # B's vocal teased over A
    probes += [hit(t, MIX, "B", start=-20) for t in (120, 150, 180)]
    b = next(e for e in build(probes, 200, W) if e.title == "B")
    assert 110 < b.start <= 120 and b.mixed_in_from == 20
