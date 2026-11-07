from tracklist_mcp.timeline import Entry, number, render
from tracklist_mcp.versions import apply, clean_title, matches


def test_clean_title():
    assert clean_title("Rodrigo Amarante - Tuyo (Narcos Theme) (SENATVS x LUMINAE Afro House Remix)") == (
        "Rodrigo Amarante",
        "Tuyo (Narcos Theme) (SENATVS x LUMINAE Afro House Remix)",
    )
    assert clean_title("Edward Maya - Stereo Love (Padé Afro House Remix) [Official Video] | Afro Vibes") == (
        "Edward Maya",
        "Stereo Love (Padé Afro House Remix)",
    )
    assert clean_title("Edward Maya – Stereo Love (Afro House Remix) 🔥") == (
        "Edward Maya",
        "Stereo Love (Afro House Remix)",
    )


def test_match_needs_both_harmony_and_timbre():
    assert matches((0.88, 0.92))
    assert not matches((0.88, 0.80))  # same chords, different production: another remix
    assert not matches((0.70, 0.95))


def test_found_version_renames_the_track_and_retires_its_stand_ins():
    tuyo = Entry(start=898, end=1022, artist="Rodrigo Amarante", title="Tuyo (Narcos Theme)", role="track", hits=4)
    stand_in = Entry(start=866, end=1075, artist="samm shay", title="Soy Yo Fuego Que", role="track", uncertain=True)
    other = Entry(start=1074, end=1232, artist="Bad Bunny", title="DtMF", role="track")
    entries = [tuyo, stand_in, other]
    found = {
        "song": "tuyo",
        "start": 898,
        "end": 1022,
        "artist": "Rodrigo Amarante",
        "title": "Tuyo (Narcos Theme) (SENATVS x LUMINAE Afro House Remix)",
        "url": "https://www.youtube.com/watch?v=fAg5fLdFr8Y",
    }
    apply(entries, [found])
    number(entries)
    assert tuyo.title.endswith("(SENATVS x LUMINAE Afro House Remix)") and tuyo.version_from == "youtube"
    assert not stand_in.confirmed and other.confirmed
    text = render(entries)
    assert "version found on YouTube" in text
    assert text.index("Possible extra layers") < text.index("Soy Yo Fuego Que")


def test_hints_come_from_the_uploaders_tracklist_and_genre():
    from tracklist_mcp.versions import hints_for

    e = Entry(start=0, end=200, artist="Rodrigo Amarante", title="Tuyo (Narcos Theme) [Extended Version]", role="track")
    description = (
        "Tracklist:\n0:00 Adam Port - Move\n14:30 Rodrigo Amarante - Tuyo (Narcos Theme) (SENATVS x LUMINAE Remix)"
    )
    hints = hints_for(e, "Afro House Mix - 2025", description)
    assert hints[0] == "Rodrigo Amarante - Tuyo (Narcos Theme) (SENATVS x LUMINAE Remix)"
    assert "Rodrigo Amarante tuyo afro house remix" in hints


def test_same_name_found_again_is_not_tagged():
    e = Entry(start=150, end=377, artist="Royal Intention", title="In My Head", role="track")
    found = {
        "song": "in my head",
        "start": 150,
        "end": 377,
        "artist": "Royal Intention",
        "title": "In My Head",
        "url": "u",
    }
    apply([e], [found])
    assert e.version_from is None and e.url == "u"


def test_names_version_requires_the_remix_credit():
    from tracklist_mcp.versions import _names_version

    line = "Sean Paul - She Doesn't Mind (Milforlife & Kajo Afro House Remix)"
    assert _names_version(line, {"title": "Sean Paul - She Doesn't Mind (Milforlife & Kajo Afro House Remix) 🐪"})
    assert not _names_version(line, {"title": "Sean Paul – She Doesn't Mind (Afro House Remix)"})


def test_listing_parses_timestamped_lines():
    from tracklist_mcp.versions import listing

    description = (
        "Tracklist:\n00:00 Intro\n22:52 Twenty 4 Seven - Leave Them Alone | 1994\n1:02:03 Artist - Late Track\nthanks!"
    )
    assert listing(description) == [(1372.0, "Twenty 4 Seven - Leave Them Alone"), (3723.0, "Artist - Late Track")]


def test_track_confirmed_from_listing_replaces_the_id_stretch():
    gap = Entry(start=1384, end=1410, artist="ID", title="ID", role="unidentified")
    before = Entry(start=1316, end=1386, artist="Mr. President", title="Up'n Away", role="track")
    entries = [before, gap]
    found = {"song": "leave them alone", "start": 1384, "end": 1410, "artist": "Twenty 4 Seven"}
    found |= {"title": "Leave Them Alone", "url": "u", "added": True}
    apply(entries, [found])
    number(entries)
    assert [e.title for e in entries] == ["Up'n Away", "Leave Them Alone"]
    assert entries[1].confirmed and "checked against the audio" in render(entries)


def test_unconfirmed_listing_is_shown_as_a_hint_and_credits_follow_the_listing():
    from tracklist_mcp.versions import orient

    vocal = Entry(start=1388, end=1407, artist="?", title="?", role="unidentified vocal", lyrics="Let's make a trip")
    swapped = Entry(start=754, end=827, artist="Close To You", title="Fun factory", role="track")
    masterboy = Entry(start=664, end=767, artist="Masterboy", title="I Got to Give It Up", role="track")
    entries = [vocal, swapped, masterboy]
    apply(entries, [{"song": None, "start": 1388, "end": 1407, "hint": "Twenty 4 Seven - Leave Them Alone"}])
    orient(
        entries,
        "11:03 Masterboy - I Got To Give It Up | 1994\n12:42 Fun Factory - Close To You | 1994\n"
        "22:52 Twenty 4 Seven - Leave Them Alone | 1994",
    )
    number(entries)
    text = render(entries)
    assert "uploader's tracklist says: Twenty 4 Seven - Leave Them Alone (not confirmed by the audio)" in text
    assert "Fun Factory - Close To You" in text


def test_title_first_listings_leave_correct_credits_alone():
    from tracklist_mcp.versions import orient, title_first

    entries = [
        Entry(start=150, end=377, artist="Royal Intention", title="In My Head", role="track"),
        Entry(start=581, end=737, artist="Stankisswell", title="Every Night", role="track"),
    ]
    description = "2:33 In My Head - Royal Intention\n9:44 Every Night - Stankisswell"
    assert title_first(entries, description)
    orient(entries, description)
    assert (entries[0].artist, entries[0].title) == ("Royal Intention", "In My Head")
