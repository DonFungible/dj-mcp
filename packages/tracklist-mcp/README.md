# tracklist-mcp

Paste a link to a DJ mix and get a timestamped tracklist, including tracks that overlap and acapellas
laid over other tracks.

```
$ tracklist "https://www.youtube.com/watch?v=dW_q6FZSDO4"

  1.   00:00–02:10    ZARG & Can U - Saint-Tropez
  2.   02:30–06:17    Royal Intention - In My Head
  3.   06:11–09:47    Key To Life - Find Our Way (Breakaway) [...] [Marc Cotterell Plastik Factory Extended Dub]
  4.   09:41–12:17    Stankisswell - Every Night
  5.   12:16–16:02    DJibouti - 1 Thing (DJibouti Dub)
  6.   15:56–18:10    ZARG - Like This
  7.   18:04–20:40    Lemtom - Bancroft
  8.   20:34–23:10    JAYC & Lemtom - Starlight  (mixed in over #7 from 20:26)
  9.   23:04–25:47    Randm - Tell Me
 10.   25:49–29:08    CHRONOSONDER - Start Movin  (mixed in over #9 from 25:32)

Possible extra layers (matched, but the matches don't line up the way a record playing does; ...):
 11.   00:00–01:55    Dee$$ - Needed You
 ...
```

And on a test mix with acapellas laid over other records (one record underneath Shazam knows, one it doesn't):

```
  2.   01:19–02:47    HUGEL, Topic & Arash - I Adore You (feat. Daecolm)  [acapella]  over #3 Stereo Love (Afro House)
  3.   01:22–02:42    Sunfloero - Stereo Love (Afro House)  [instrumental]
  4.   02:41–04:02    Bad Bunny - DtMF
  5.   03:56–05:10    Rihanna - Only Girl (SHVS Remix)  [acapella]  over music Shazam couldn't name
```

The start time is when a record takes over. "mixed in from" is when it was really started underneath the
previous one, worked out from how far into the record Shazam placed it.


It works as a command-line tool and as an [MCP](https://modelcontextprotocol.io) server, so you can ask Claude
*"what's the tracklist of https://youtu.be/…?"*

## How it works

1. **Download** the audio with [yt-dlp](https://github.com/yt-dlp/yt-dlp). Anything yt-dlp supports works
   (YouTube, SoundCloud, Mixcloud, …), and so does a local audio file.
2. **Split the vocals from the music** with [Demucs](https://github.com/facebookresearch/demucs). This runs on
   your GPU (Apple Silicon or CUDA) and takes about 3 minutes for a 2-hour set.
3. **Fingerprint the full mix** with Shazam (see sampling below).
4. **Fingerprint the vocal stem** wherever someone is singing. An acapella over another record gets drowned out
   in the full mix, but on the vocal stem it's on its own.
5. **Fingerprint the instrumental stem** under every recognised vocal, to find the record underneath. A loud
   acapella can dominate the full mix too, so this is the only way to tell an acapella from a vocal track.
6. **Compare with the released record** where Shazam can't name the music under a vocal (it often can't name
   an instrumental on its own). The tool fetches the vocal's song from YouTube, splits off its instrumental and
   compares harmony with the music in the mix, allowing for tempo and pitch changes. The record's own music
   scores ~0.95; a different record scores well below. A clear mismatch means an acapella over something else.
7. **Name the exact version.** Shazam often knows only the original of a remix a DJ plays. For each track, the
   version Shazam named is fetched from YouTube and compared with the mix; if it isn't what's playing, remixes
   are searched for (using the uploader's own tracklist and the mix's genre as hints) and compared the same way.
   Harmony alone can't tell remixes of one song apart, so timbre and rhythm are compared too, and a remix is
   accepted only when it clearly matches and clearly beats the others. These are marked "version found on
   YouTube".
8. **Use the uploader's tracklist, checked against the audio.** If the video description has a timestamped
   tracklist, it's used to fill stretches nothing matched: each line near that time is searched for on YouTube
   and compared with the mix, and only a match is added (marked as such). An unconfirmed line is shown as a hint
   ("uploader's tracklist says ...") rather than as a track. The tracklist's credit order (artist first or title
   first) is worked out from the tracks already identified.
9. **Transcribe vocals that nothing matched** with Whisper, so you (or Claude) can look them up by lyrics.
10. **Build the timeline.** Samples of the same song close together become one entry. Remixes and edits of a song
   count as the same song. Entries can overlap. One-off matches in the middle of another record are dropped as
   misfires.

Sampling is coarse-to-fine: every 60 seconds first, and wherever two neighbouring samples aren't the same record
playing straight through (Shazam says how far into the record each sample is), it samples halfway between, down
to 7.5 seconds. Transitions get pinned down without spending lookups on the middle of every track.

Each entry is one of:

| Label | Meaning |
| --- | --- |
| *(none)* | a track, heard in the full mix |
| `[acapella]` | a vocal over a different record: named if Shazam knows it, otherwise "over music Shazam couldn't name" |
| `[instrumental]` | heard only on the instrumental stem, under someone else's vocal |
| `ID - ID (not identified)` | 40+ seconds nothing matched: an unreleased track, an edit, or something Shazam doesn't know |
| `Unidentified vocal` | singing nothing matched, with the transcribed lyrics |

**Possible extra layers** are listed after the tracklist, not in it. These are records Shazam matched whose
matches never agree on when the record started. A record that's really playing gives matches whose position in
the record advances with the clock; a loop or sample shared with another record (common in house music) gives
matches stuck at one spot or jumping around. Occasionally one is a real short layer, so they're shown, apart.

`(uncertain: ...)` marks a record whose matches don't move forward through the record the way a record playing
straight through does. That usually means a remix or edit Shazam doesn't know, matched to pieces of other
releases; the name shown is the closest match, not necessarily what was played.

## Accuracy

Checked against DJs' own published tracklists:

| Mix | Published | Found | Extra entries in the tracklist | Start times |
| --- | --- | --- | --- | --- |
| [ZARA deep house mix](https://www.youtube.com/watch?v=dW_q6FZSDO4) (29 min) | 10 | 10 | 0 | within 0–13 s |
| [Afro House Mix 2025](https://www.youtube.com/watch?v=XX8GPmM6lZs) (26 min) | 8 | 8 | 2 | within 2–28 s |
| [90s Eurodance Megamix](https://www.youtube.com/watch?v=jNpEfVx63F8) (30 min, incl. an acapella) | 21 | 21 | 0 | median 8 s; 20 of 21 within 20 s |

- **Eurodance megamix**: 21 records in 30 minutes, often under a minute each. The published acapella comes out as
  *Urban Cookie Collective - The Key, The Secret [acapella] over Tribal Dance*, which is what the DJ played. One
  track Shazam doesn't know (*Twenty 4 Seven - Leave Them Alone*) was filled in from the uploader's tracklist
  after its audio was compared with uploads of that song. The one start time off by more than 20 s is the first
  track, which the published list dates after a 45-second intro made of snippets.
- **Afro House mix**: five of the eight are remixes Shazam only knows as the original. All five are named exactly
  as the DJ lists them, by comparing YouTube uploads with the mix: Milforlife & Kajo's *She Doesn't Mind*, Padé's
  *Stereo Love*, SENATVS x LUMINAE's *Tuyo*, and Tasty Or Not's *DtMF* and *NINAO*. The two extras are a closing
  track the published list leaves out (*Show Me Love*, three minutes of it) and a 35-second intro.
- **ZARA deep house mix**: Shazam matched 20 other records on loops these tracks share; all of them are kept out
  of the tracklist (listed apart as possible extra layers).

Acapellas over records Shazam can't name, key-shifted acapellas and unreleased vocals were checked on test mixes
built from known material: all were found, the unreleased vocal as "unidentified vocal" with its lyrics.

## Speed

Shazam limits how many lookups it answers, and that sets the pace. The limit varies: sometimes about 20 a minute,
sometimes 3 every 40 seconds or so. The tool slows down when refused and speeds back up after a run of successes.
Expect **10–20 minutes for a one-hour mix** when Shazam is generous, and up to about twice the mix's length when
it isn't. A 26-minute mix took 53 minutes on a heavily throttled day (177 lookups).

Every answer is saved, so if it's interrupted, running the same command again carries on where it stopped. A
finished tracklist is saved too and comes back instantly next time.

`--fast` samples every 2 minutes and skips lyrics. It needs about half the lookups but can miss a record played
for under two minutes.

## Install

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and `ffmpeg` (`brew install ffmpeg` on
macOS). A JavaScript runtime (`node`, `deno` or `bun`) lets yt-dlp read YouTube reliably.

```sh
uvx --from tracklist-mcp tracklist "https://www.youtube.com/watch?v=…"
```

The first run downloads PyTorch, the Demucs model (~80 MB) and the Whisper model (~500 MB).

### As an MCP server

```sh
claude mcp add tracklist -- uvx tracklist-mcp
```

or in Claude Desktop's config:

```json
"tracklist": { "command": "uvx", "args": ["tracklist-mcp"] }
```

Tools:

- `identify_mix(url)` starts the job in the background and returns right away.
- `get_tracklist(url)` returns progress, then the tracklist.

## Options

| Flag | Default | |
| --- | --- | --- |
| `--hop SECONDS` | 60 | time between first-pass samples of the mix |
| `--fast` | | `--hop 120`, coarser refinement, no lyrics |
| `--no-stems` | | skip vocal separation (no acapella detection) |
| `--no-lyrics` | | skip transcription |
| `--json` | | machine-readable output |
| `--refresh` | | ignore the saved tracklist and redo it |
| `--no-versions` | | don't search YouTube for the exact remix |
| `--keep-audio` | | keep the downloaded audio and stems |

Environment variables: `TRACKLIST_MCP_CACHE` (where work is saved; default `~/Library/Caches/tracklist-mcp` on
macOS, `~/.cache/tracklist-mcp` on Linux) and `TRACKLIST_WHISPER_MODEL` (default `small`).

## Limits

- Shazam knows released music. Unreleased IDs, bootlegs and live edits usually come back as `ID - ID`.
- It uses Shazam's unofficial API (via [shazamio](https://github.com/shazamio/shazamio)), which may change or
  stop working. It stays within the rate Shazam accepts.
- Remixes are often reported as the original mix of the song, since that's what's in Shazam's catalogue.
- Very short layers (a vocal chop of a few seconds) are below what a 12-second fingerprint can catch.
- When Shazam can't name the music under an acapella, the acapella is still found by comparing with the released
  record, but the music underneath shows as "music Shazam couldn't name". If the released record can't be found
  on YouTube, or it's a different version of the song, the comparison is inconclusive and the entry stays a
  normal track rather than guessing.
- Lyrics are transcribed only for vocals nothing matched, and only when Whisper is fairly sure; it returns nothing
  rather than guessing.
