#!/usr/bin/env python3
"""Offline video handoff: one timeline, readable transcript, metadata candidates.

No download, rendering, approval, CMS or publishing operations. Python stdlib only.
"""
import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def url_groups(urls):
    """Keep originals; ignore tracking and fragments, preserve functional queries."""
    groups = {}
    for original in urls:
        p = urlsplit(original)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
            raise ValueError('Source URL must be HTTP(S) without credentials')
        query = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            if k.lower() in ('token', 'access_token', 'key', 'signature', 'auth'):
                raise ValueError('Do not place credential-bearing URLs in the handoff')
            if not k.lower().startswith('utm_') and k.lower() not in ('gclid', 'fbclid'):
                query.append((k, v))
        key = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/',
                          urlencode(sorted(query)), ''))
        groups.setdefault(key, []).append(original)
    return [{'normalized_url': k, 'original_urls': v,
             'decision': 'HOLD: inspect the current page before choosing reuse or a new URL'}
            for k, v in groups.items()]


def validate(data):
    if data['schema_version'] != '1.0':
        raise ValueError('Unsupported schema_version')
    track = data.get('caption_track')
    if (not isinstance(track, dict) or not isinstance(track.get('language'), str)
            or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', track['language'])
            or not isinstance(track.get('label'), str) or not track['label'].strip()):
        raise ValueError('caption_track requires a language tag (e.g. en or zh-CN) and a nonempty label')
    duration = data['duration_ms']
    if type(duration) is not int or not 0 < duration <= 30000:
        raise ValueError('This preflight accepts 1–30000 ms samples only')
    if data['mode'] not in ('fixture', 'approved_sample'):
        raise ValueError('mode must be fixture or approved_sample')
    if data['mode'] == 'fixture' and data.get('synthetic') is not True:
        raise ValueError('Fixture must be explicitly synthetic')
    if data['mode'] == 'approved_sample':
        for name in ('G1', 'G2'):
            gate = data['gates'][name]
            if gate.get('status') != 'PASS' or not all(gate.get(k) for k in ('actor', 'evidence', 'artifact_sha256')):
                raise ValueError(f'{name} HOLD: factual/keyframe approval evidence required')
        if data['asset_scope'].get('status') != 'approved' or not data['asset_scope'].get('evidence'):
            raise ValueError('Approved material scope evidence required')
    previous_end = 0
    for shot in data['shots']:
        start, end = shot['start_ms'], shot['end_ms']
        if type(start) is not int or type(end) is not int or start != previous_end or not start < end <= duration:
            raise ValueError('Shots must cover the timeline continuously with integer milliseconds')
        previous_end = end
    if previous_end != duration:
        raise ValueError('Shots must cover the full duration')
    previous_end = 0
    for cue in data['captions']:
        start, end = cue['start_ms'], cue['end_ms']
        if type(start) is not int or type(end) is not int or not previous_end <= start < end <= duration:
            raise ValueError('Captions must be ordered, non-overlapping and inside the timeline')
        if not cue['text'].strip() or '-->' in cue['text'] or '\n' in cue['text'] or '\r' in cue['text']:
            raise ValueError('Caption text must be one nonempty line without a VTT separator')
        previous_end = end
    if data['audio']['kind'] not in ('silent', 'synthetic_mix', 'approved_mix'):
        raise ValueError('Declare the actual audio kind')
    if data['audio']['offset_ms'] != 0:
        raise ValueError('Embedded audio must use the muxed timeline origin; retain stream offsets from probe')
    return url_groups(data['original_urls'])


def probe(media, duration_ms, audio):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                        '-of', 'json', str(media)], capture_output=True, text=True,
                       check=True, timeout=30)
    info = json.loads(p.stdout)
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    has_audio = any(s['codec_type'] == 'audio' for s in info['streams'])
    if abs(float(info['format']['duration']) * 1000 - duration_ms) > 100:
        raise ValueError('Manifest duration differs from media by more than 100 ms')
    if has_audio != (audio['kind'] != 'silent'):
        raise ValueError('Audio declaration differs from actual streams')
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(media), '-map', '0:v:0',
                    '-map', '0:a?', '-f', 'null', '-'], capture_output=True,
                   check=True, timeout=60)
    return {'duration_s': float(info['format']['duration']), 'width': video['width'],
            'height': video['height'], 'frame_rate': video['r_frame_rate'],
            'has_audio': has_audio, 'streams': info['streams'], 'decode': 'PASS',
            'browser_play_seek': 'NOT_TESTED', 'human_listen': 'NOT_TESTED',
            'mobile_readability': 'NOT_TESTED'}


def timestamp(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f'{h:02}:{m:02}:{s:02}.{milli:03}'


def build(manifest, out):
    manifest, out = Path(manifest).resolve(), Path(out).resolve()
    # Exclusive directory creation preserves previous receipts and original inputs.
    out.mkdir(parents=True, exist_ok=False)
    receipt = {'status': 'BLOCKED', 'source_manifest': str(manifest),
               'created_at': datetime.now(timezone.utc).isoformat(),
               'next_owner': 'project_owner',
               'external_mutation': False, 'approval_changed': False,
               'approval_verification': 'INPUT_DECLARATIONS_ONLY',
               'publication_authorized': False}
    try:
        receipt['source_manifest_sha256'] = digest(manifest)
        data = json.loads(manifest.read_text())
        groups = validate(data)
        media = (manifest.parent / data['media']['path']).resolve()
        if digest(media) != data['media']['sha256']:
            raise ValueError('Media SHA256 mismatch; stop before decode or packaging')
        qa = probe(media, data['duration_ms'], data['audio'])
        shutil.copy2(media, out / 'sample.mp4')
        shutil.copy2(manifest, out / 'input.json')
        vtt = 'WEBVTT\n\n' + '\n\n'.join(
            f"{timestamp(c['start_ms'])} --> {timestamp(c['end_ms'])}\n{c['text']}"
            for c in data['captions']) + '\n'
        (out / 'captions.vtt').write_text(vtt)
        transcript = '\n'.join(c['text'] for c in data['captions'])
        metadata = {'publish_ready': False, 'status': 'candidate_only',
                    'caption_track': data['caption_track'],
                    'missing_for_publication': ['uploadDate', 'thumbnailUrl', 'contentUrl or embedUrl',
                                                'current URL decision', 'Owner approval'],
                    'VideoObject_candidate': {'@type': 'VideoObject', 'name': data['title'],
                        'description': data['description'],
                        'duration': f"PT{data['duration_ms'] / 1000:g}S", 'transcript': transcript}}
        save(out / 'metadata-candidate.json', metadata)
        save(out / 'url-review.json', groups)
        save(out / 'playback-check.json', qa)
        save(out / 'timeline.json', {k: data[k] for k in
             ('revision', 'duration_ms', 'shots', 'audio', 'caption_track', 'captions', 'gates', 'asset_scope')})
        e = html.escape
        body = ''.join(f"<p><time>{timestamp(c['start_ms'])}–{timestamp(c['end_ms'])}</time> {e(c['text'])}</p>"
                       for c in data['captions'])
        banner = 'SYNTHETIC TEST ONLY - not a production video' if data['mode'] == 'fixture' else 'Sample preflight using approval declarations; Owner verification still required'
        page = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(data['title'])}</title>
<style>body{{max-width:900px;margin:24px auto;padding:0 18px;font:17px/1.7 system-ui;color:#172d35}}video{{width:100%;background:#111}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}time{{font-variant-numeric:tabular-nums;color:#53636b}}.tag{{background:#fff1c6;padding:12px}}</style>
<h1>{e(data['title'])}</h1><p class="tag">{banner}</p><p>{e(data['description'])}</p>
<video controls preload="metadata"><source src="sample.mp4" type="video/mp4"><track kind="captions" src="captions.vtt" srclang="{e(data['caption_track']['language'])}" label="{e(data['caption_track']['label'])}" default></video>
<p>Audio: {e(data['audio']['kind'])}. Decode PASS; browser playback/seek, mobile readability and listening remain unreviewed.</p>
<h2>Readable transcript</h2>{body}<h2>Shot timeline</h2><pre>{e(json.dumps(data['shots'], ensure_ascii=False, indent=2))}</pre>
<h2>Original URL grouping and page reuse review</h2><pre>{e(json.dumps(groups, ensure_ascii=False, indent=2))}</pre>
<h2>Video metadata candidates</h2><p>Candidate fields only; not deployed structured data or evidence of indexing.</p><pre>{e(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre>
<h2>Review declarations and recovery</h2><pre>{e(json.dumps(data['gates'], ensure_ascii=False, indent=2))}</pre>
<p>G1/G2 checks cover input declarations only. The Owner must independently verify actual signoff, artifact hash, revision and scope. This result grants no publication permission.</p>
<p>Preserve existing review requirements. On failure, inspect recovery-receipt.json before using a new output directory. The project Owner retains acceptance and merge authority.</p></html>'''
        (out / 'report.html').write_text(page)
        receipt.update(status='PASS_TECHNICAL_ONLY', business_video=False if data['mode'] == 'fixture' else None,
                       source_media_sha256=data['media']['sha256'],
                       next_action='Independent review; browser play/seek and mobile/listen review before any G3 signoff',
                       stop_conditions=['G1/G2 HOLD', 'hash mismatch', 'missing approval scope',
                                        'access denial: do not retry or switch channels'])
    except Exception as exc:
        receipt.update(error=f'{type(exc).__name__}: {exc}',
                       next_action='Inspect this receipt and partial files; fix input or obtain Owner evidence; use a new output directory')
    receipt['completed_files'] = [{'file': p.name, 'sha256': digest(p)}
                                  for p in sorted(out.iterdir()) if p.is_file()]
    save(out / 'recovery-receipt.json', receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        result = build(args.manifest, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result['status'] == 'PASS_TECHNICAL_ONLY' else 1)
    except FileExistsError:
        print('BLOCKED: output directory already exists; inspect previous receipt, then choose a new directory')
        raise SystemExit(1)
