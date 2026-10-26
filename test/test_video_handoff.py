import copy
import importlib.util
import json
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
        }))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.data = json.loads(self.manifest.read_text())

    def test_original_url_dedup_preserves_variants_and_inputs(self):
        groups = h.validate(self.data)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]['original_urls'], self.data['original_urls'][:2])
        self.assertTrue(all(g['decision'].startswith('HOLD') for g in groups))

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
            source.write_text(json.dumps(self.data))
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
            (out / 'recovery-receipt.json').write_text('preserve original receipt')
            with self.assertRaises(FileExistsError):
                h.build(self.manifest, out)
            self.assertEqual((out / 'recovery-receipt.json').read_text(), 'preserve original receipt')

    def test_real_decode_and_packaged_transcript_do_not_claim_playback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            result = h.build(self.manifest, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            self.assertFalse(result['business_video'])
            qa = json.loads((out / 'playback-check.json').read_text())
            self.assertEqual(qa['decode'], 'PASS')
            self.assertTrue(qa['has_audio'])
            self.assertEqual(qa['browser_play_seek'], 'NOT_TESTED')
            page = (out / 'report.html').read_text()
            for text in ('SYNTHETIC INTRO', 'SYNTHETIC MIDDLE', 'SYNTHETIC END', 'not a production video'):
                self.assertIn(text, page)
            vtt = (out / 'captions.vtt').read_text()
            self.assertIn('00:00:04.250 --> 00:00:06.000', vtt)
            metadata = json.loads((out / 'metadata-candidate.json').read_text())
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
            source.write_text(json.dumps(self.data, ensure_ascii=False))
            out = Path(tmp) / 'out'
            result = h.build(source, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            parser = TrackParser()
            parser.feed((out / 'report.html').read_text())
            self.assertEqual(parser.tracks[0]['srclang'], 'zh-CN')
            self.assertEqual(parser.tracks[0]['label'], '简体中文')
            self.assertIn('合成校准字幕，仅供测试', (out / 'captions.vtt').read_text())
            for name in ('timeline.json', 'metadata-candidate.json'):
                exported = json.loads((out / name).read_text())
                self.assertEqual(exported['caption_track'], self.data['caption_track'])
            self.assertNotIn('inLanguage', json.loads((out / 'metadata-candidate.json').read_text())['VideoObject_candidate'])

    def test_gate_declarations_do_not_become_release_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['mode'] = 'approved_sample'
            for gate in ('G1', 'G2'):
                self.data['gates'][gate] = {'status': 'PASS', 'actor': 'synthetic fixture',
                    'evidence': 'declaration only, not a real signoff', 'artifact_sha256': 'synthetic'}
            self.data['asset_scope'] = {'status': 'approved', 'evidence': 'synthetic declaration only'}
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data))
            out = Path(tmp) / 'out'
            result = h.build(source, out)
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY', result)
            self.assertEqual(result['approval_verification'], 'INPUT_DECLARATIONS_ONLY')
            self.assertFalse(result['publication_authorized'])
            page = (out / 'report.html').read_text()
            self.assertIn('Owner verification still required', page)
            self.assertIn('This result grants no publication permission', page)
            self.assertNotIn('approved materials preflight', page)

    def test_html_source_text_is_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['title'] = '<script>alert(1)</script>'
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data))
            result = h.build(source, Path(tmp) / 'out')
            self.assertEqual(result['status'], 'PASS_TECHNICAL_ONLY')
            page = (Path(tmp) / 'out/report.html').read_text()
            self.assertNotIn('<script>', page)
            self.assertIn('&lt;script&gt;', page)

    def test_silent_declaration_cannot_disguise_existing_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data['audio']['kind'] = 'silent'
            self.data['media']['path'] = str(self.media)
            source = Path(tmp) / 'input.json'
            source.write_text(json.dumps(self.data))
            result = h.build(source, Path(tmp) / 'out')
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertIn('Audio declaration', result['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
