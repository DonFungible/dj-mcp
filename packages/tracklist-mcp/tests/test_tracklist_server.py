import json
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tracklist_mcp import pipeline, server
from tracklist_mcp.audio import AudioError, MixInfo, mix_id
from tracklist_mcp.recognize import Match
from tracklist_mcp.timeline import Entry

URL = "https://www.youtube.com/watch?v=XX8GPmM6lZs"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKLIST_MCP_CACHE", str(tmp_path))
    server._jobs.clear()
    return tmp_path


def fake_result() -> pipeline.Result:
    entries = [
        Entry(start=0, end=200, artist="Adam Port", title="Move", role="track", hits=9, sources={"mix": 9}, n=1),
        Entry(start=150, end=230, artist="Rihanna", title="Only Girl", role="acapella", hits=3, n=2, layered_with=[1]),
    ]
    return pipeline.Result(MixInfo(id="yt-x", title="Test mix", url=URL, uploader=None, duration=240), entries, {})


def test_mix_id_is_stable_across_link_forms():
    assert mix_id(URL) == mix_id("https://youtu.be/XX8GPmM6lZs?t=30") == "yt-XX8GPmM6lZs"


def test_job_runs_in_background_then_returns_tracklist(monkeypatch, cache):
    gate = threading.Event()

    def fake_run(source, options, progress):
        progress("Identifying the mix: 3/10")
        gate.wait(5)
        result = fake_result()
        (cache / mix_id(source)).mkdir(parents=True, exist_ok=True)
        (cache / mix_id(source) / "result.json").write_text(json.dumps(result.to_dict()))
        return result

    monkeypatch.setattr(pipeline, "run", fake_run)
    assert server.identify_mix(URL)["status"] == "started"
    assert server.identify_mix(URL)["status"] == "running"
    status = server.get_tracklist(URL)
    assert status["status"] == "running"
    gate.set()
    server._jobs[mix_id(URL)].thread.join(5)

    done = server.get_tracklist("https://youtu.be/XX8GPmM6lZs")
    assert done["status"] == "done"
    assert "Only Girl  [acapella]  over #1 Move" in done["tracklist"]
    # A finished mix is answered from disk without starting another job.
    assert server.identify_mix(URL)["status"] == "done"


def test_errors_reach_the_client(monkeypatch):
    def failing(source, options, progress):
        raise AudioError("Couldn't download it")

    monkeypatch.setattr(pipeline, "run", failing)
    server.identify_mix(URL)
    server._jobs[mix_id(URL)].thread.join(5)
    with pytest.raises(ToolError, match="Couldn't download"):
        server.get_tracklist(URL)


def test_unknown_link():
    with pytest.raises(ToolError, match="identify_mix first"):
        server.get_tracklist("https://youtu.be/AAAAAAAAAAA")


def test_match_from_shazam_response():
    r = {
        "matches": [{"id": "1", "offset": 48.6, "timeskew": -0.024, "frequencyskew": -0.001}],
        "track": {"key": "813929913", "title": "NINAO", "subtitle": "GIMS", "url": "https://www.shazam.com/track/1"},
    }
    m = Match.from_response(r)
    assert (m.artist, m.title, m.offset) == ("GIMS", "NINAO", 48.6)
    assert Match.from_response({"matches": [], "tagid": "x"}) is None
