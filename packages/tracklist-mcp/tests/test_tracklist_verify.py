import numpy as np

from tracklist_mcp.verify import RATE, chroma, similarity, verdict


def chords(progression: list[list[float]], seconds: float = 2.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    out = [sum(np.sin(2 * np.pi * f * t) for f in chord) + 0.05 * rng.standard_normal(len(t)) for chord in progression]
    return np.concatenate(out).astype(np.float32)


C, F, G, AM = [261.6, 329.6, 392.0], [349.2, 440.0, 523.3], [392.0, 493.9, 587.3], [220.0, 261.6, 329.6]
EB, AB, BB = [311.1, 392.0, 466.2], [415.3, 523.3, 622.3], [466.2, 587.3, 698.5]


def test_same_record_scores_high_even_when_sped_up():
    record = chords([C, F, G, AM] * 4)
    excerpt = record[4 * RATE : 16 * RATE]
    faster = np.interp(np.arange(0, len(excerpt), 1.04), np.arange(len(excerpt)), excerpt).astype(np.float32)
    assert verdict(similarity(chroma(excerpt), chroma(record))) == "own"
    assert verdict(similarity(chroma(faster), chroma(record))) == "own"


def test_different_record_scores_low():
    record = chords([C, F, G, AM] * 4)
    other = chords([EB, AB, BB, EB] * 2, seed=1)[: 12 * RATE]
    assert verdict(similarity(chroma(other), chroma(record))) == "other"
