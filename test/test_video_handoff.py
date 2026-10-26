import copy
import html
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1] / 'tools/video-handoff'
spec = importlib.util.spec_from_file_location('handoff', ROOT / 'handoff.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class TrackParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tracks = []

    def handle_starttag(self, tag, attrs):
        if tag == 'track':
            self.tracks.append(dict(attrs))


class HandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        folder = Path(cls.fixture_dir.name)
        cls.media = folder / 'synthetic.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=navy:s=64x64:r=2:d=8', '-f', 'lavfi', '-i',
                        'sine=frequency=880:sample_rate=8000:duration=8',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                        '-shortest', str(cls.media)], check=True, capture_output=True)
        cls.manifest = folder / 'input.json'
        cls.manifest.write_text(json.dumps({
            'schema_version': '1.0', 'mode': 'fixture', 'synthetic': True,
            'revision': 'synthetic-test-v1', 'title': 'Synthetic handoff test',
            'description': 'Generated color and tone for regression tests only.',
            'caption_track': {'language': 'en', 'label': 'English'},
            'duration_ms': 8000,
            'media': {'path': cls.media.name, 'sha256': h.digest(cls.media)},
            'asset_scope': {'status': 'synthetic_test_only', 'evidence': 'local lavfi fixture'},
            'gates': {'G1': {'status': 'HOLD'}, 'G2': {'status': 'HOLD'}},
            'audio': {'kind': 'synthetic_mix', 'offset_ms': 0},
            'shots': [{'id': 'S0', 'start_ms': 0, 'end_ms': 2000},
                      {'id': 'S1', 'start_ms': 2000, 'end_ms': 8000}],
            'captions': [{'start_ms': 2000, 'end_ms': 4000, 'text': 'SYNTHETIC INTRO'},
                         {'start_ms': 4250, 'end_ms': 6000, 'text': 'SYNTHETIC MIDDLE'},
                         {'start_ms': 6000, 'end_ms': 7800, 'text': 'SYNTHETIC END'}],
            'original_urls': ['https://example.org/sample?utm_source=test#t=2',
                              'https://example.org/sample',
                              'https://example.org/sample?variant=2']
        }), encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.data = json.loads(self.manifest.read_text(encoding='utf-8'))

    def test_original_url_dedup_preserves_variants_and_inputs(self):
        groups = h.validate(self.data)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]['original_urls'], self.data['original_urls'][:2])
        self.assertTrue(all(g['decision'].startswith('HOLD') for g in groups))

    def test_repeated_functional_parameter_order_survives_tracking_removal(self):
        urls = [
            'https://example.org/sample?filter=crop&filter=scale&utm_source=first',
            'https://example.org/sample?filter=scale&filter=crop&utm_source=second',
            'https://example.org/sample?utm_medium=email&filter=crop&utm_campaign=test&filter=scale#top',
            'https://example.org/sample?filter=scale&utm_medium=email&filter=crop',
        ]
        groups = h.url_groups(urls)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]['original_urls'], [urls[0], urls[2]])
        self.assertEqual(groups[1]['original_urls'], [urls[1], urls[3]])
        self.assertEqual(groups[0]['normalized_url'], 'https://example.org/sample?filter=crop&filter=scale')
        self.assertEqual(groups[1]['normalized_url'], 'https://example.org/sample?filter=scale&filter=crop')

    def test_shots_cannot_hide_a_timeline_gap(self):
        self.data['shots'][1]['start_ms'] = 2100
        with self.assertRaisesRegex(ValueError, 'continuously'):
            h.validate(self.data)

    def test_subtitle_overrun_and_overlap_are_rejected(self):
        for end in (8500, 5000):
            with self.subTest(end=end):
                data = copy.deepcopy(self.data)
                data['captions'][0]['end_ms'] = end
                with self.assertRaisesRegex(ValueError, 'Captions'):
                    h.validate(data)

    def test_millisecond_type_and_fixture_label_are_explicit(self):
        self.data['duration_ms'] = float('nan')
        with self.assertRaises(ValueError):
            h.validate(self.data)
        self.setUp()
        self.data['synthetic'] = False
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            h.validate(self.data)

    def test_unapproved_sample_stays_on_hold(self):
        self.data['mode'] = 'approved_sample'
        for gate in ('G1', 'G2'):
            with self.subTest(gate=gate):
                with self.assertRaisesRegex(ValueError, gate):
                    h.validate(self.data)
                self.data['gates'][gate] = {'status': 'PASS', 'actor': 'fixture',
                    'evidence': 'declaration only', 'artifact_sha256': 'fixture'}
        with self.assertRaisesRegex(ValueError, 'scope'):
            h.validate(self.data)

    def test_credential_urls_never_enter_outputs(self):
        for url in ('https://user:secret@example.org/', 'https://example.org/?access_token=secret'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    h.url_groups([url])

    def test_corrupt_media_hash_produces_recovery_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.json'
            self.data['media']['path'] = str(self.media)
            self.data['media']['sha256'] = '0' * 64
            source.write_text(json.dumps(self.data), encoding='utf-8')
            result = h.build(source, Path(tmp) / 'out')
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertIn('SHA256 mismatch', result['error'])
            self.assertFalse(result['external_mutation'])
            self.assertTrue((Path(tmp) / 'out/recovery-receipt.json').is_file())
            self.assertFalse((Path(tmp) / 'out/sample.mp4').exists())

    def test_existing_receipt_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            out.mkdir()
            (out / 'recovery-receipt.json').write_text('preserve original receipt', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                h.build(self.manifest, out)
            self.assertEqual((out / 'recovery-receipt.json').read_text(encoding='utf-8'), 'preserve original receipt')

    def test_real_decode_and_packaged_transcript_do_not_claim_playback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            result = h.build(self.manifest, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            self.assertFalse(result['business_video'])
            qa = json.loads((out / 'playback-check.json').read_text(encoding='utf-8'))
            self.assertEqual(qa['decode'], 'PASS')
            self.assertTrue(qa['has_audio'])
            self.assertEqual(qa['browser_play_seek'], 'NOT_TESTED')
            page = (out / 'report.html').read_text(encoding='utf-8')
            for text in ('SYNTHETIC INTRO', 'SYNTHETIC MIDDLE', 'SYNTHETIC END', 'not a production video'):
                self.assertIn(text, page)
            vtt = (out / 'captions.vtt').read_text(encoding='utf-8')
            self.assertIn('00:00:04.250 --> 00:00:06.000', vtt)
            metadata = json.loads((out / 'metadata-candidate.json').read_text(encoding='utf-8'))
            self.assertFalse(metadata['publish_ready'])
            parser = TrackParser()
            parser.feed(page)
            self.assertEqual(parser.tracks[0]['srclang'], 'en')
            self.assertEqual(parser.tracks[0]['label'], 'English')
            self.assertEqual(metadata['caption_track'], self.data['caption_track'])
            for key in ('contentUrl', 'embedUrl', 'uploadDate', 'thumbnailUrl'):
                self.assertNotIn(key, metadata['VideoObject_candidate'])

    def test_caption_language_is_explicit_at_entry(self):
        for track in (None, {}, {'language': 'zh_CN', 'label': '中文'},
                      {'language': 'zh-CN', 'label': ''},
                      {'language': 123, 'label': '中文'}):
            with self.subTest(track=track):
                data = copy.deepcopy(self.data)
                if track is None:
                    del data['caption_track']
                else:
                    data['caption_track'] = track
                with self.assertRaisesRegex(ValueError, 'caption_track'):
                    h.validate(data)

    def test_chinese_track_and_transcript_survive_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['caption_track'] = {'language': 'zh-CN', 'label': '简体中文'}
            self.data['captions'][0]['text'] = '合成校准字幕，仅供测试'
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
            out = Path(tmp) / 'out'
            result = h.build(source, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            parser = TrackParser()
            parser.feed((out / 'report.html').read_text(encoding='utf-8'))
            self.assertEqual(parser.tracks[0]['srclang'], 'zh-CN')
            self.assertEqual(parser.tracks[0]['label'], '简体中文')
            self.assertIn('合成校准字幕，仅供测试', (out / 'captions.vtt').read_text(encoding='utf-8'))
            for name in ('timeline.json', 'metadata-candidate.json'):
                exported = json.loads((out / name).read_text(encoding='utf-8'))
                self.assertEqual(exported['caption_track'], self.data['caption_track'])
            self.assertNotIn('inLanguage', json.loads((out / 'metadata-candidate.json').read_text(encoding='utf-8'))['VideoObject_candidate'])

    def test_gate_declarations_do_not_become_release_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['mode'] = 'approved_sample'
            for gate in ('G1', 'G2'):
                self.data['gates'][gate] = {'status': 'PASS', 'actor': 'synthetic fixture',
                    'evidence': 'declaration only, not a real signoff', 'artifact_sha256': 'synthetic'}
            self.data['asset_scope'] = {'status': 'approved', 'evidence': 'synthetic declaration only'}
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data), encoding='utf-8')
            out = Path(tmp) / 'out'
            result = h.build(source, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            self.assertEqual(result['approval_verification'], 'INPUT_DECLARATIONS_ONLY')
            self.assertFalse(result['publication_authorized'])
            page = (out / 'report.html').read_text(encoding='utf-8')
            self.assertIn('Owner verification still required', page)
            self.assertIn('This result grants no publication permission', page)
            self.assertNotIn('approved materials preflight', page)

    def test_html_source_text_is_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['title'] = '<script>alert(1)</script>'
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data), encoding='utf-8')
            result = h.build(source, Path(tmp) / 'out')
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY')
            page = (Path(tmp) / 'out/report.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>', page)
            self.assertIn('&lt;script&gt;', page)

    def test_silent_declaration_cannot_disguise_existing_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['audio']['kind'] = 'silent'
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data), encoding='utf-8')
            result = h.build(source, Path(tmp) / 'out')
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertIn('Audio declaration', result['error'])

    def test_vtt_preserves_literal_caption_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = 'Use <placeholder> &amp; "literal" text'
            self.data['captions'][0]['text'] = literal
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data), encoding='utf-8')
            out = Path(tmp) / 'out'
            self.assertEqual(h.build(source, out)['status'], 'PASS_TECHNICAL_ONLY')
            cue = (out / 'captions.vtt').read_text(encoding='utf-8').splitlines()[3]
            self.assertNotIn('<placeholder>', cue)
            self.assertEqual(html.unescape(cue), literal)
            self.assertIn('&amp;amp;', cue)
            metadata = json.loads((out / 'metadata-candidate.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['VideoObject_candidate']['transcript'].splitlines()[0], literal)

    def test_packaged_manifest_can_be_moved_and_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source_dir = folder / 'source'
            source_dir.mkdir()
            shutil.copy2(self.media, source_dir / 'original.mp4')
            self.data['media']['path'] = 'original.mp4'
            source = source_dir / 'input.json'
            source.write_text(json.dumps(self.data), encoding='utf-8')
            original = source.read_bytes()
            first = folder / 'first'
            self.assertEqual(h.build(source, first)['status'], 'PASS_TECHNICAL_ONLY')
            moved = folder / 'moved'
            shutil.move(first, moved)
            shutil.rmtree(source_dir)
            copied = json.loads((moved / 'input.json').read_text(encoding='utf-8'))
            self.assertTrue((moved / copied['media']['path']).is_file())
            self.assertEqual((moved / 'source-input.json').read_bytes(), original)
            second = folder / 'second'
            result = h.build(moved / 'input.json', second)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            self.assertEqual(h.digest(second / 'sample.mp4'), self.data['media']['sha256'])

    def test_utf8_files_under_an_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['caption_track'] = {'language': 'zh-CN', 'label': '简体中文'}
            self.data['title'] = '多语言校准'
            self.data['captions'][0]['text'] = '中文与 café，仅供测试'
            self.data['media']['path'] = str(self.media)
            env = dict(os.environ, LC_ALL='C', LANG='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
            child = ('import codecs, locale, runpy, sys; '
                     'assert codecs.lookup(locale.getpreferredencoding(False)).name == "ascii"; '
                     'cli = sys.argv.pop(1); sys.argv[0] = cli; '
                     'runpy.run_path(cli, run_name="__main__")')
            for escaped in (False, True):
                with self.subTest(ascii_escaped_input=escaped):
                    source = Path(tmp) / f'input-{escaped}.json'
                    source.write_text(json.dumps(self.data, ensure_ascii=escaped), encoding='utf-8')
                    out = Path(tmp) / f'out-{escaped}'
                    result = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', child,
                                             str(ROOT / 'handoff.py'), str(source), '--out', str(out)],
                                            env=env, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                    self.assertIn('中文与 café，仅供测试', (out / 'captions.vtt').read_bytes().decode('utf-8'))
                    self.assertIn('多语言校准', (out / 'report.html').read_bytes().decode('utf-8'))
                    timeline = json.loads((out / 'timeline.json').read_bytes().decode('utf-8'))
                    self.assertEqual(timeline['caption_track']['label'], '简体中文')


    def test_cli_receipt_json_on_ascii_stdout_with_unicode_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / '中文路径'
            folder.mkdir()
            self.data['media']['path'] = str(self.media)
            self.data['caption_track'] = {'language': 'zh-CN', 'label': '简体中文'}
            self.data['captions'][0]['text'] = '中文校准字幕'
            env = dict(os.environ, PYTHONIOENCODING='ascii', PYTHONUTF8='1')
            child = ('import runpy, sys; assert sys.stdout.encoding.lower() == "ascii"; '
                     'cli = sys.argv.pop(1); sys.argv[0] = cli; '
                     'runpy.run_path(cli, run_name="__main__")')
            for valid in (True, False):
                with self.subTest(valid_media_hash=valid):
                    data = copy.deepcopy(self.data)
                    if not valid:
                        data['media']['sha256'] = '0' * 64
                    source = folder / f'输入-{valid}.json'
                    source.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                    out = folder / f'输出-{valid}'
                    result = subprocess.run([sys.executable, '-c', child,
                                             str(ROOT / 'handoff.py'), str(source), '--out', str(out)],
                                            env=env, capture_output=True, timeout=30)
                    saved = json.loads((out / 'recovery-receipt.json').read_text(encoding='utf-8'))
                    self.assertEqual(saved['status'], 'PASS_TECHNICAL_ONLY' if valid else 'BLOCKED')
                    self.assertEqual(result.returncode, 0 if valid else 1,
                                     f"saved_status={saved['status']}; " + result.stderr.decode('utf-8', errors='replace'))
                    console = json.loads(result.stdout.decode('ascii'))
                    self.assertEqual(console, saved)
                    self.assertEqual(console['source_manifest'], str(source.resolve()))
                    self.assertFalse(console['publication_authorized'])
                    if valid:
                        self.assertIn('中文校准字幕', (out / 'captions.vtt').read_text(encoding='utf-8'))
                    else:
                        self.assertIn('SHA256 mismatch', console['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
