"""Offline enrollment regressions with synthetic clips, identities and model vectors."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'meeting-transcribe' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import config
import localio
import report
import review
import sampling
import transcribe as cli
import voiceprint as vp


class SamplingFlowTests(unittest.TestCase):
    """Exercise the local CLI through completion without cloud, decoding or inference."""

    def setUp(self):
        """Own every input and library; prevent accidental access to user configuration."""
        temporary = tempfile.TemporaryDirectory(prefix='meeting-sampling-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            'AGENT_SKILLS_CONFIG_DIR': str(self.root / 'config'),
            'MEETING_TRANSCRIBE_DATA_DIR': str(self.root / 'data'),
            'MEETING_TRANSCRIBE_CACHE_DIR': str(self.root / 'cache'),
            'HOME': str(self.root), 'USERPROFILE': str(self.root)}, clear=True))
        self.cfg = config.Config(vp_enabled=False, vp_min_seconds=1, vp_segment_seconds=4,
                                 vp_db=str(self.root / 'voices.json'),
                                 roster_path=str(self.root / 'names.json'))
        self.stack.enter_context(patch.object(cli, 'load_config', return_value=self.cfg))
        self.stack.enter_context(patch.object(cli, 'ensure_tools', return_value=('ffmpeg', 'ffprobe')))
        self.stack.enter_context(patch.object(cli, 'resolve_vp_switch', return_value=True))
        self.stack.enter_context(patch.object(cli.bailian, '_request', side_effect=AssertionError('network')))
        self.raw = self.root / 'meeting.asr.json'
        self.raw.write_text(json.dumps({'transcripts': [{'sentences': [
            {'speaker_id': sid, 'begin_time': sid * 10000, 'end_time': sid * 10000 + 8000,
             'text': f'示例发言{sid}'} for sid in range(2)]}]}), encoding='utf-8')
        self.audio = self.root / 'meeting.mp3'
        self.audio.write_bytes(b'synthetic-source')
        self.model = self.root / 'test-model.onnx'
        self.model.write_bytes(b'synthetic-model')
        self.stages = review.artifact_paths(self.raw)

    def run_cli(self, *args):
        """Capture actual CLI output and fail with the complete diagnostic on errors."""
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = cli.main(['--from-json', str(self.raw), *args])
        self.assertEqual(result, 0, output.getvalue())
        return output.getvalue()

    def freeze(self, suggestions=None):
        """Write a real immutable first-attribution snapshot from simulated threshold results."""
        with patch.object(cli, 'run_voiceprint', return_value=(suggestions or {}, {}, self.audio)):
            self.run_cli()

    @staticmethod
    def clip(audio, dst, start, duration, ffmpeg):
        """Generate deterministic fake MP3 bytes solely for digest and ownership checks."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f'synthetic-{start}-{duration}'.encode())

    def export(self, *args):
        """Run real span selection and manifest generation, replacing only the encoder."""
        with patch.object(sampling, '_clip', side_effect=self.clip) as encoder:
            output = self.run_cli('--continue', *args)
        return localio.read_json(self.stages['samples']), encoder.call_count, output

    def test_reject_then_accept_finishes_without_extracting_replacements(self):
        """Keep rejected clips deleted while enrolling only the remaining accepted group."""
        self.freeze()
        original, count, _ = self.export('--speaker-map', '0=示例甲,1=示例乙')
        self.assertEqual(count, 4)
        with (patch.object(sampling, '_clip', side_effect=AssertionError('unexpected re-extraction')),
             patch.object(cli, 'run_voiceprint', side_effect=AssertionError('unexpected rematch')),
             patch.object(vp, 'ensure_model', return_value=self.model),
             patch.object(vp, 'embed_audio', return_value=[1., 0., 0.]) as embed):
            rejected_output = self.run_cli('--samples-reject', '0')
            self.assertEqual(embed.call_count, 0)
            self.assertNotIn(original['groups'][0]['files'][0]['path'], rejected_output)
            self.assertNotIn('[report]', rejected_output)
            output = self.run_cli('--samples-accept', '1')
            self.assertEqual(embed.call_count, 2)
            self.assertIn('[report]', output)
            self.run_cli('--continue')
        current = localio.read_json(self.stages['samples'])
        self.assertEqual(current['identity'], original['identity'])
        self.assertEqual([g['status'] for g in current['groups']], ['rejected', 'accepted'])
        self.assertFalse(any(Path(f['path']).exists() for f in current['groups'][0]['files']))
        self.assertEqual([(s['name'], len(s['samples'])) for s in vp.load_db(self.cfg.vp_db)['speakers']],
                         [('示例乙', 2)])
        self.assertTrue(report.context(self.raw)['source'].is_file())

    def test_threshold_identity_skips_export_and_enrollment_while_low_match_does_not(self):
        """A low candidate manually named by the user still requires listening and approval."""
        self.freeze({0: [('示例甲', .9, True)], 1: [('示例乙', .3, False)]})
        data, count, _ = self.export('--speaker-map', '1=示例乙')
        self.assertEqual(count, 2)
        self.assertEqual([g['status'] for g in data['groups']], ['skipped', 'pending'])
        self.assertEqual(data['groups'][0]['files'], [])
        with (patch.object(vp, 'ensure_model', return_value=self.model),
              patch.object(vp, 'embed_audio', return_value=[1., 0., 0.]) as embed):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--from-json', str(self.raw), '--samples-accept', '0']), 2)
            self.assertEqual(embed.call_count, 0)
            output = self.run_cli('--samples-accept', '1')
            self.assertEqual(embed.call_count, 2)
        self.assertIn('[report]', output)
        self.assertEqual([s['name'] for s in vp.load_db(self.cfg.vp_db)['speakers']], ['示例乙'])
        self.assertTrue(report.context(self.raw)['source'].is_file())

    def test_all_threshold_matches_complete_without_any_sampling_or_inference(self):
        """All-skipped is a completed batch and permits minutes immediately."""
        self.freeze({0: [('示例甲', .9, True)], 1: [('示例乙', .8, True)]})
        with (patch.object(vp, 'pick_spans', side_effect=AssertionError('unexpected span selection')),
             patch.object(vp, 'ensure_model', side_effect=AssertionError('unexpected model')),
             patch.object(vp, 'embed_audio', side_effect=AssertionError('unexpected inference'))):
            data, count, output = self.export()
            repeated, repeated_count, _ = self.export('--resample')
        self.assertEqual(count, 0)
        self.assertEqual(repeated_count, 0)
        self.assertTrue(all(g['status'] == 'skipped' for g in data['groups'] + repeated['groups']))
        self.assertIn('[report]', output)
        self.assertNotIn('[review]', output)
        self.assertFalse(Path(self.cfg.vp_db).exists())
        self.assertTrue(report.context(self.raw)['source'].is_file())

    def test_corrected_identity_still_gets_samples(self):
        """Correcting a mistaken threshold identity to someone else never skips that new person."""
        self.freeze({0: [('示例甲', .9, True)]})
        data, count, _ = self.export('--speaker-map', '0=更正姓名,1=示例乙')
        self.assertEqual(count, 4)
        self.assertEqual([g['name'] for g in data['groups']], ['更正姓名', '示例乙'])
        self.assertTrue(all(g['status'] == 'pending' for g in data['groups']))

    def test_existing_pending_batch_skips_known_person_without_replacing_other_clips(self):
        """Upgrade an older pending manifest in place and preserve its batch and other files."""
        self.freeze({0: [('示例甲', .9, True)]})
        with patch.object(sampling, 'matched_names', return_value=set()):
            old, _, _ = self.export('--speaker-map', '1=示例乙')
        with patch.object(sampling, '_clip', side_effect=AssertionError('unexpected re-extraction')):
            self.run_cli('--continue')
            self.run_cli('--continue')
        new = localio.read_json(self.stages['samples'])
        self.assertEqual(new['identity'], old['identity'])
        self.assertEqual(new['groups'][0]['status'], 'skipped')
        self.assertFalse(any(Path(f['path']).exists() for f in old['groups'][0]['files']))
        self.assertEqual(new['groups'][1], old['groups'][1])
        self.assertTrue(all(Path(f['path']).is_file() for f in new['groups'][1]['files']))

    def test_snapshot_requires_a_real_direct_threshold_assignment(self):
        """Names alone or a matching lower-ranked candidate cannot justify skipping enrollment."""
        record = {'names': {'0': '示例甲', '1': '手动姓名', '2': '低分姓名'}, 'suggestions': {
            '0': [['示例甲', .9, True]], '1': [['其他候选', .9, True]],
            '2': [['低分姓名', .2, False], ['低分姓名', .1, True]]}}
        self.assertEqual(sampling.matched_names(record), {'示例甲'})
        self.assertEqual(sampling.matched_names({'names': {'0': '示例甲'}}), set())


if __name__ == '__main__':
    unittest.main()
