import pytest

from spotify_dj_mcp import server
from spotify_dj_mcp.matching import Matcher, artist_set, split_title

LIB = [
    {
        "id": "1",
        "title": "Lost For Words (Snatt & Vix Remix)",
        "artist": "Ferry Corsten",
        "bpm": 130.0,
        "camelot": "10A",
    },
    {"id": "2", "title": "Waterpark (Original Mix)", "artist": "Kölsch", "bpm": 132.0, "camelot": "10A"},
    {"id": "3", "title": "STAY [Explicit]", "artist": "The Kid LAROI & Justin Bieber", "bpm": 170.0, "camelot": "4B"},
    {"id": "4", "title": "Do Do Do (Extended Mix)", "artist": "Sam Divine", "bpm": 135.0, "camelot": "11A"},
    {"id": "5", "title": "Lost For Words", "artist": "Ferry Corsten", "bpm": 128.0, "camelot": "9A"},
    {"id": "6", "title": "Top Of The World (Ferry Tayle Remix)", "artist": "Somebody", "bpm": 138.0, "camelot": "10A"},
]


def test_split_title():
    assert split_title("Savana - Extended Mix") == ("savana", "")
    assert split_title("STAY [Explicit]") == ("stay", "")
    assert split_title("Lost For Words (Snatt & Vix Remix)") == ("lost for words", "snatt vix remix")
    assert split_title("Song (feat. Someone) - Radio Edit") == ("song", "")
    assert split_title("01 - Track Name") == ("track name", "")
    assert split_title("Café Del Mar - 2026 Remaster") == ("cafe del mar", "")
    assert split_title("STAY (with Justin Bieber)") == ("stay", "")


def test_artist_set():
    assert artist_set(["The Kid LAROI", "Justin Bieber"]) == {"the kid laroi", "justin bieber"}
    assert artist_set("A & B feat. C") == {"a", "b", "c"}
    assert artist_set("Kölsch") == {"kolsch"}


@pytest.mark.parametrize(
    "name,artists,want_id,confidence",
    [
        ("Waterpark", ["Kölsch"], "2", "exact"),  # Spotify radio title vs "(Original Mix)"
        ("Waterpark - Extended Mix", ["Kolsch"], "2", "exact"),  # accent folding
        ("STAY (with Justin Bieber)", ["The Kid LAROI", "Justin Bieber"], "3", "exact"),
        ("Lost For Words", ["Ferry Corsten"], "5", "exact"),  # original, not the remix
        ("Lost For Words - Snatt & Vix Remix", ["Ferry Corsten", "Snatt & Vix"], "1", "exact"),
        ("Do Do Do", ["Sam Divine"], "4", "exact"),
    ],
)
def test_matches(name, artists, want_id, confidence):
    m = Matcher(LIB).match(name, artists)
    assert m and m.track.id == want_id and m.confidence == confidence, m


def test_non_matches():
    mt = Matcher(LIB)
    assert mt.match("Waterpark", ["Someone Else"]) is None  # same title, wrong artist
    assert mt.match("Completely Different", ["Kölsch"]) is None
    m = mt.match("Top Of The World", ["Somebody"])  # only a remix in the library
    assert m and m.confidence == "different version"


class FakeClient:
    def liked_songs(self, limit):
        names = [
            ("Waterpark", "Kölsch"),
            ("Lost For Words", "Ferry Corsten"),
            ("Do Do Do", "Sam Divine"),
            ("Top Of The World", "Somebody"),
            ("Not In Library", "Nobody"),
            ("Waterpark", "Kölsch"),
        ]
        return [
            {"id": f"s{i}" if i < 5 else "s0", "uri": f"spotify:track:s{i if i < 5 else 0}", "name": n, "artists": [a]}
            for i, (n, a) in enumerate(names)
        ][:limit]

    def playlist(self, ref):
        return {"name": "PL"}

    def playlist_tracks(self, ref, limit):
        return [
            {
                "id": "s9",
                "uri": "spotify:track:s9",
                "name": "Lost For Words - Snatt & Vix Remix",
                "artists": ["Ferry Corsten"],
            }
        ]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(server, "client", lambda: FakeClient())
    monkeypatch.setattr(server, "_rekordbox_tracks", lambda: LIB)


def test_build_mix(fake):
    res = server.build_mix(["liked", "https://open.spotify.com/playlist/abcdefghijk"], curve="build")
    s = res["summary"]
    assert s["spotify_songs"] == 6  # duplicate liked song removed
    assert s["matched_in_rekordbox"] == 4 and s["not_in_library"] == 1 and s["different_version_only"] == 1
    bpms = [t["bpm"] for t in res["tracks"]]
    assert bpms[0] == min(bpms)
    assert res["rekordbox_track_ids"] == [t["id"] for t in res["tracks"]]
    assert len(res["spotify_uris"]) == 4
    assert res["not_in_library"][0]["spotify_name"] == "Not In Library"


def test_build_mix_with_versions_and_limit(fake):
    res = server.build_mix(["liked"], include_different_versions=True, max_tracks=2)
    assert res["summary"]["used_in_set"] == 2 and res["different_version_only"] == []
