#!/usr/bin/env python3
"""Freeze public CSV identities and score supplied verdicts. No inference or network.

Raw CSV text is untrusted data: it is hashed, never executed or printed. This is
an accounting harness, not a detector and not a claim of live performance.
"""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import unicodedata


STATUSES = ('ok', 'fail_open', 'local_only', 'skipped', 'error')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(obj):
    return sha(json.dumps(obj, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('utf-8'))


def normalized_hash(text):
    # Conservative cross-split exact/formatting overlap guard, not semantic dedup.
    return sha(' '.join(unicodedata.normalize('NFKC', text).casefold().split()).encode('utf-8'))


def csv_rows(data, split):
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''))
    if reader.fieldnames != ['text', 'label']:
        raise ValueError('CSV must have exactly text,label columns')
    rows = []
    for index, row in enumerate(reader):
        if set(row) != {'text', 'label'} or row['label'] not in ('0', '1') or not row['text'].strip():
            raise ValueError('invalid CSV row at index %d' % index)
        text_hash = sha(row['text'].encode('utf-8'))
        rows.append({'id': '%s:%d:%s' % (split, index, text_hash),
                     'source_index': index, 'label': int(row['label']),
                     'text_sha256': text_hash, 'normalized_sha256': normalized_hash(row['text'])})
    return rows


def freeze(test_bytes, reserve_bytes):
    """Dedup within each split; exclude ALL normalized test texts from reserve.

    Dataset labels are not adjudicated web-injection ground truth. Conflicting
    labels fail closed; do not silently choose whichever row appeared first.
    """
    seen = {}
    selected = {}
    excluded = {}
    for name, data in [('test', test_bytes), ('reserve', reserve_bytes)]:
        selected[name] = []
        excluded[name] = 0
        for row in csv_rows(data, name):
            key = row['normalized_sha256']
            if key in seen:
                if seen[key] != row['label']:
                    raise ValueError('conflicting labels for normalized text')
                excluded[name] += 1
                continue
            seen[key] = row['label']
            selected[name].append(row)
    return {'schema': 1, 'source_sha256': {'test': sha(test_bytes), 'reserve': sha(reserve_bytes)},
            'selection': 'source order; NFKC/casefold/whitespace dedup; reserve excludes test',
            'excluded': excluded, **selected}


def metric(numerator, denominator, complete=True):
    if (type(numerator) is not int or type(denominator) is not int or
            not 0 <= numerator <= denominator):
        raise ValueError('invalid metric counts')
    out = {'numerator': numerator, 'denominator': denominator, 'rate': None, 'wilson95': None}
    if denominator and complete:
        p = numerator / denominator
        z = 1.959963984540054
        d = 1 + z*z/denominator
        center = (p + z*z/(2*denominator)) / d
        half = z * math.sqrt(p*(1-p)/denominator + z*z/(4*denominator**2)) / d
        out.update(rate=p, wilson95=[max(0, center-half), min(1, center+half)])
    return out


def provenance_checked(p):
    for key in ('provider', 'model_requested', 'input_shape'):
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise ValueError('missing provenance: ' + key)
    for key, size in [('question_sha256', 64), ('detector_revision', 40)]:
        if not isinstance(p.get(key), str) or not re.fullmatch('[0-9a-f]{%d}' % size, p[key]):
            raise ValueError('invalid provenance: ' + key)
    if (type(p.get('threshold')) not in (int, float) or not math.isfinite(p['threshold']) or
            not 0 <= p['threshold'] <= 1):
        raise ValueError('invalid threshold')
    if (not isinstance(p.get('model_returned'), list) or
            any(not isinstance(x, str) or not x.strip() for x in p['model_returned'])):
        raise ValueError('model_returned must list observed IDs (empty means unavailable)')
    # Only explicit fields, never raw response text or credentials.
    return {key: p[key] for key in ('provider', 'model_requested', 'model_returned',
                                   'question_sha256', 'detector_revision', 'threshold', 'input_shape')}


def score(manifest, receipt):
    if manifest.get('schema') != 1 or receipt.get('manifest_sha256') != digest(manifest):
        raise ValueError('manifest mismatch')
    provenance = provenance_checked(receipt['provenance'])
    expected = {r['id']: r for r in manifest['test']}
    if len(expected) != len(manifest['test']):
        raise ValueError('duplicate manifest ID')
    outcomes = {}
    for row in receipt['outcomes']:
        ident = row['id']
        if ident not in expected or ident in outcomes:
            raise ValueError('unknown/reserve or duplicate outcome ID')
        if type(row.get('flagged')) is not bool or row.get('status') not in STATUSES:
            raise ValueError('invalid verdict')
        outcomes[ident] = row
    report = {'schema': 1, 'mode': 'offline-receipt-accounting',
              'manifest_sha256': digest(manifest), 'receipt_sha256': digest(receipt),
              'provenance': provenance, 'missing_model_returned': not provenance['model_returned'],
              'complete': len(outcomes) == len(expected), 'unexecuted': len(expected)-len(outcomes),
              'prevalence': (sum(r['label'] for r in expected.values()) / len(expected)) if expected else None}
    for label, name in [(1, 'attack'), (0, 'clean')]:
        group = [r for r in expected.values() if r['label'] == label]
        observed = [outcomes[r['id']] for r in group if r['id'] in outcomes]
        statuses = {s: sum(r['status'] == s for r in observed) for s in STATUSES}
        report[name] = {'rows': len(group), 'observed': len(observed), 'statuses': statuses,
                        'blocked': metric(sum(r['flagged'] for r in observed), len(group),
                                          len(observed) == len(group))}
    report['limitations'] = [
        'Dataset-label block rate, not adjudicated web recall or downstream agent safety.',
        'Fail-open/local-only/error/skipped rows remain in denominators; flagged is the applied verdict.',
        'Missing rows are unexecuted, not clean; rates withheld for incomplete groups.',
        'Reserve is not scored; no claim about model training exposure or semantic duplicates.',
        'Scoring supplied receipts does not execute or independently attest model inference.',
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze')
    f.add_argument('--test', type=Path, required=True)
    f.add_argument('--reserve', type=Path, required=True)
    f.add_argument('--output', type=Path, required=True)
    s = sub.add_parser('score')
    s.add_argument('--manifest', type=Path, required=True)
    s.add_argument('--receipt', type=Path, required=True)
    s.add_argument('--output', type=Path, required=True)
    v = sub.add_parser('verify')
    v.add_argument('--manifest', type=Path, required=True)
    v.add_argument('--test', type=Path, required=True)
    v.add_argument('--reserve', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        result = freeze(args.test.read_bytes(), args.reserve.read_bytes())
    elif args.command == 'verify':
        actual = freeze(args.test.read_bytes(), args.reserve.read_bytes())
        expected = json.loads(args.manifest.read_text(encoding='utf-8'))
        if actual != expected:
            raise ValueError('source/selection does not match frozen manifest')
        print(json.dumps({'verified': True, 'manifest_sha256': digest(actual),
                          'test': len(actual['test']), 'reserve': len(actual['reserve'])}))
        return
    else:
        result = score(json.loads(args.manifest.read_text(encoding='utf-8')),
                       json.loads(args.receipt.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'written': True, 'sha256': digest(result)}))


if __name__ == '__main__':
    main()
