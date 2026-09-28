"""Offline accounting tests, not evidence of detector quality."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    'heldout', Path(__file__).resolve().parents[1] / 'scripts/webscreen_heldout.py')
assert SPEC is not None and SPEC.loader is not None
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


class HeldoutTests(unittest.TestCase):
    def setUp(self):
        self.test = b'text,label\nattack sample,1\nclean sample,0\nattack sample,1\n'
        self.reserve = b'text,label\nattack sample,1\nnew sample,1\nother sample,0\n'
        self.manifest = h.freeze(self.test, self.reserve)
        self.provenance = {
            'provider': 'fixture-only', 'model_requested': 'fixture',
            'model_returned': [], 'question_sha256': 'a' * 64,
            'detector_revision': 'b' * 40, 'threshold': 0.5,
            'input_shape': 'single row as web_extract content',
        }

    def receipt(self, outcomes):
        return {'manifest_sha256': h.digest(self.manifest),
                'provenance': self.provenance, 'outcomes': outcomes}

    def outcomes(self):
        return [{'id': r['id'], 'flagged': r['label'] == 1, 'status': 'ok'}
                for r in self.manifest['test']]

    def test_freeze_deduplicates_and_excludes_test_from_reserve(self):
        self.assertEqual(len(self.manifest['test']), 2)
        self.assertEqual(len(self.manifest['reserve']), 2)
        self.assertFalse({r['text_sha256'] for r in self.manifest['test']} &
                         {r['text_sha256'] for r in self.manifest['reserve']})
        self.assertNotIn('attack sample', json.dumps(self.manifest))
        self.assertEqual(self.manifest, h.freeze(self.test, self.reserve))

    def test_normalized_overlap_is_excluded(self):
        m = h.freeze(b'text,label\nHello,1\n', b'text,label\n HELLO ,1\n')
        self.assertEqual(m['reserve'], [])

    def test_label_conflict_rejected(self):
        with self.assertRaises(ValueError):
            h.freeze(b'text,label\nsame,1\nsame,0\n', self.reserve)
        with self.assertRaises(ValueError):
            h.freeze(self.test, b'text,label\nattack sample,0\n')

    def test_bad_csv_rejected(self):
        for data in [b'text,label\nx,2\n', b'text,label\n,0\n',
                     b'wrong,label\nx,1\n', b'text,label\nx,1,extra\n']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                h.freeze(data, self.reserve)

    def test_recall_and_fp_separate(self):
        report = h.score(self.manifest, self.receipt(self.outcomes()))
        self.assertEqual(report['attack']['blocked']['rate'], 1)
        self.assertEqual(report['clean']['blocked']['rate'], 0)
        self.assertEqual(report['prevalence'], 0.5)
        self.assertEqual(report['missing_model_returned'], True)
        self.assertGreater(report['clean']['blocked']['wilson95'][1], 0)

    def test_fail_open_not_dropped_from_denominator(self):
        rows = self.outcomes()
        rows[0].update(status='fail_open', flagged=False)
        report = h.score(self.manifest, self.receipt(rows))
        self.assertEqual(report['attack']['blocked']['denominator'], 1)
        self.assertEqual(report['attack']['blocked']['rate'], 0)
        self.assertEqual(report['attack']['statuses']['fail_open'], 1)

    def test_applied_fallback_flag_is_retained(self):
        rows = self.outcomes()
        rows[0]['status'] = 'fail_open'
        report = h.score(self.manifest, self.receipt(rows))
        self.assertEqual(report['attack']['blocked']['numerator'], 1)

    def test_missing_is_not_clean_or_live_success(self):
        report = h.score(self.manifest, self.receipt([]))
        self.assertFalse(report['complete'])
        self.assertIsNone(report['attack']['blocked']['rate'])
        self.assertIsNone(report['clean']['blocked']['wilson95'])
        self.assertEqual(report['unexecuted'], 2)

    def test_manifest_mismatch_and_unknown_duplicate_rows_rejected(self):
        receipt = self.receipt(self.outcomes())
        receipt['manifest_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            h.score(self.manifest, receipt)
        for rows in [self.outcomes() * 2,
                     [{'id': 'unknown', 'flagged': False, 'status': 'ok'}],
                     [{'id': self.manifest['reserve'][0]['id'], 'flagged': False, 'status': 'ok'}]]:
            with self.assertRaises(ValueError):
                h.score(self.manifest, self.receipt(rows))

    def test_invalid_verdict_and_provenance_rejected(self):
        for key, value in [('flagged', 'false'), ('status', 'maybe')]:
            rows = self.outcomes()
            rows[0][key] = value
            with self.assertRaises(ValueError):
                h.score(self.manifest, self.receipt(rows))
        for key, value in [('threshold', float('nan')), ('question_sha256', ''),
                           ('model_returned', 'not-a-list')]:
            receipt = copy.deepcopy(self.receipt(self.outcomes()))
            receipt['provenance'][key] = value
            with self.assertRaises(ValueError):
                h.score(self.manifest, receipt)

    def test_wilson_known_and_empty(self):
        metric = h.metric(15, 40)
        self.assertAlmostEqual(metric['rate'], .375)
        self.assertAlmostEqual(metric['wilson95'][0], .2422, places=3)
        self.assertAlmostEqual(metric['wilson95'][1], .5297, places=3)
        self.assertIsNone(h.metric(0, 0)['rate'])
        for x, n in [(2, 1), (-1, 1), (True, 1)]:
            with self.assertRaises(ValueError):
                h.metric(x, n)


class RecountTests(unittest.TestCase):
    def test_committed_manifest_is_frozen_and_disjoint(self):
        path = Path(__file__).resolve().parents[1] / 'evals/web-screen/heldout-manifest-v1.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(h.digest(manifest),
                         'ccdb9061dc195bb7f620025dbf19752bb855ae4f027624c29e01f5d1c44e103b')
        self.assertEqual(len(manifest['test']), 2101)
        self.assertEqual(len(manifest['reserve']), 1985)
        self.assertFalse({r['normalized_sha256'] for r in manifest['test']} &
                         {r['normalized_sha256'] for r in manifest['reserve']})

    def test_checksum_gate_rejects_tampering(self):
        import sys
        import tempfile
        from unittest import mock
        with mock.patch.dict(sys.modules, {'webscreen_heldout': h}):
            spec = importlib.util.spec_from_file_location(
                'recount', Path(__file__).resolve().parents[1] / 'scripts/webscreen_recount_issue25.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            for name in module.SOURCES:
                Path(directory, name).write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                module.recount(Path(directory))


if __name__ == '__main__':
    unittest.main()
