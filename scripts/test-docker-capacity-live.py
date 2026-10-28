#!/usr/bin/env python3
"""Measure a bounded healthy window for the isolated gallery fleet; always drain afterward."""
import argparse
import json
import time
from pathlib import Path

from test_docker_fleet_live import Test

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise', action='store_true', required=True)
    parser.add_argument('--config', type=Path, default=ROOT / '.state/gallery/config.json')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--hold-seconds', type=int, default=30)
    args = parser.parse_args()
    test = Test(args.config, args.port)
    if not 1 <= args.workers <= test.config['max_workers'] or args.hold_seconds < 30:
        parser.error('Worker count must fit the configured ceiling; hold at least 30 seconds')
    if test.api()['counts']['active'] or test.api()['desired']:
        raise ValueError('Capacity test requires a running, drained controller')
    report = {'passed': False, 'workers': args.workers, 'holdSeconds': args.hold_seconds,
              'profile': test.config, 'samples': []}
    output = test.state.parent / 'capacity-results.json'
    def save():
        output.write_text(json.dumps(report, indent=2) + '\n')
    try:
        test.api('/api/fleet/resume', {})
        test.api('/api/fleet', {'desired': args.workers})
        test.wait(lambda: len(test.healthy()) == args.workers, 'full fleet measured healthy', 480)
        expected = {w['runId'] for w in test.active()}
        started = time.monotonic()
        while time.monotonic() - started < args.hold_seconds:
            workers = test.active()
            if {w['runId'] for w in workers} != expected or not all(w['healthy'] for w in workers):
                raise AssertionError('Fleet lost a worker or measured health during capacity window')
            report['samples'].append({'wall': time.time(), 'workers': [
                {'runId': w['runId'], 'backendId': w['backendId'], 'health': w['liveHealth']}
                for w in workers]})
            save()
            time.sleep(2)
        report['passed'] = True
        print(f'Passed: {args.workers} measured healthy workers for {args.hold_seconds}s', flush=True)
    finally:
        test.api('/api/fleet', {'desired': 0})
        test.wait(lambda: test.api()['counts']['active'] == 0, 'capacity cleanup drain', 180)
        report['drained'] = True
        report['wall'] = time.time()
        save()


if __name__ == '__main__':
    main()
