# Offline video handoff

An opt-in Python adapter for local samples up to 30 seconds. It checks the input
media hash, timeline, declared audio streams and full FFmpeg decode, then writes
an HTML transcript/player, WebVTT captions, timeline JSON, metadata candidates,
original URL groups and a recovery receipt. It does not render, download,
change project review status, publish, or grant approval.

Requirements: Python 3.11+, FFmpeg and ffprobe. Tests generate a temporary neutral
color/tone fixture with FFmpeg; no media assets or external downloads are needed.

```sh
python3 -m unittest discover -s test -p 'test_video_handoff.py' -v
python3 tools/video-handoff/handoff.py /path/to/input.json --out /path/to/new-output
```

The test fixture's `setUpClass` shows a complete manifest. Required fields include
`schema_version: "1.0"`, mode, revision, title, description, duration_ms, media
(path and SHA256), shots, audio, caption_track, captions, asset_scope, gates and
original_urls. Times are integer milliseconds on the muxed video's origin.
Shots must cover the full timeline; caption gaps are allowed, overlap is not.
`caption_track` requires a language tag and a nonempty display label, for example
`{"language":"en","label":"English"}` or
`{"language":"zh-CN","label":"简体中文"}`. The entry check validates tag shape
once, without guessing a language or falling back to English. It populates the
HTML track and JSON sidecars, not the video's spoken-language metadata.
Manifest reads and generated text files use UTF-8 explicitly. Caption text stays
literal in WebVTT, including angle brackets and entity-like text such as `&amp;`.

`mode=fixture` requires `synthetic=true`. `mode=approved_sample` requires complete
G1/G2 declarations and an approved asset-scope declaration. These are declaration
checks only: the project Owner must independently verify real signoff, reviewed
hash, revision and permitted scope. `PASS_TECHNICAL_ONLY` and a zero exit status
grant no publishing permission. Browser play/seek, mobile readability, listening,
scientific facts and indexing remain outside these checks.

Each run requires a new output directory. On failure inspect recovery-receipt.json
before retrying. `source-input.json` preserves the submitted manifest bytes;
`input.json` is a runnable copy that references the packaged `sample.mp4`.
It can be moved with the package and rerun without the original media directory.
Keep sample.mp4
and captions.vtt beside report.html when serving it through your usual local HTTP
workflow. Metadata remains a candidate sidecar; it does not deploy live JSON-LD.
URL grouping preserves originals and functional query variants, removes common
tracking parameters and fragments, and leaves page reuse decisions for review.
Functional parameter order is preserved, including repeated keys. Reordered
functional queries are conservatively kept in separate groups.

This adapter is independent of the drawing kit and does not change the examples.

The `Video handoff tests` workflow runs this same test file on relevant pull
requests to main and relevant pushes to main. It checks out the exact PR head
(or push SHA), limits checkout to the four tool/test files, installs FFmpeg from
Ubuntu's package repositories and uses Python 3.11+ on a standard Ubuntu runner.
It has read-only repository contents access, does not persist Git credentials,
and uses no custom secrets, paid runner, package service or manual dispatch.
The workflow file itself is also a trigger path. A local test pass or an empty
checks list is not a remote CI pass; review the run for the actual head before
considering a merge.
