import json
import tempfile
import unittest
from pathlib import Path

from conductor.artifacts import quality_report
from conductor.organize_proof import organize_server_confirmed
from conductor.storage import atomic_json, artifact_manifest


def evidence():
    before = ['air:0'] * 41
    before[8] = 'stone_pickaxe:1'
    after = before.copy(); after[1], after[8] = after[8], after[1]
    return {'activity': 'organize_inventory', 'source': 'focused_activity_task', 'status': 'completed',
        'goalStartedWall': 100.0, 'goalFinishedWall': 101.0,
        'evidence': {'mechanics': [{'id': 'inventory_organize', 'success': True, 'goalTaskFinished': True,
            'actionObserved': True, 'source': 'client_action_and_task_outcome',
            'beforeSlots': before, 'afterSlots': after,
            'packetDeltas': {'SERVERBOUND:minecraft:container_click': 2}}]}}


def sample(wall, uuid, slots):
    return {'source': 'fabric_end_server_tick', 'wall': wall,
            'playerSnapshots': [{'uuid': uuid, 'inventorySlots': slots}]}


def write_samples(path, records):
    (path / 'server-heartbeats.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))


class OrganizeProofTests(unittest.TestCase):
    def test_matches_real_server_change_without_any_server_slot_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); activity = evidence(); event = activity['evidence']['mechanics'][0]
            write_samples(root, [sample(99, 'bot', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])])
            self.assertTrue(organize_server_confirmed(root, activity, 'bot'))

    def test_rejects_noop_wrong_uuid_stale_or_mismatched_layout_and_missing_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); activity = evidence(); event = activity['evidence']['mechanics'][0]
            def valid(): return [sample(99, 'bot', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])]
            for rows in ([sample(99, 'other', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])],
                         [sample(94, 'bot', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])],
                         [sample(99, 'bot', event['beforeSlots']), sample(112, 'bot', event['afterSlots'])],
                         [sample(99, 'bot', event['beforeSlots']), sample(102, 'bot', event['beforeSlots'])]):
                write_samples(root, rows); self.assertFalse(organize_server_confirmed(root, activity, 'bot'))
            write_samples(root, valid()); event['afterSlots'] = event['beforeSlots'].copy()
            self.assertFalse(organize_server_confirmed(root, activity, 'bot'))
            event['afterSlots'] = evidence()['evidence']['mechanics'][0]['afterSlots']
            event['packetDeltas'] = {'CLIENTBOUND:minecraft:container_set_slot': 1}
            self.assertFalse(organize_server_confirmed(root, activity, 'bot'))

    def test_rejects_malformed_slots_and_nonconserving_inventory_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ('stone_pickaxe:2', 'stone_pickaxe:0', 'air:1', 'stone_pickaxe:-1', 'stone_pickaxe:01', 'bad'):
                activity = evidence(); event = activity['evidence']['mechanics'][0]
                event['afterSlots'][1] = bad
                write_samples(root, [sample(99, 'bot', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])])
                self.assertFalse(organize_server_confirmed(root, activity, 'bot'), bad)

    def test_admission_recomputes_server_evidence_and_keeps_failed_run_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'captures').mkdir()
            (root / 'captures/a.bin').write_bytes(b'xx')
            (root / 'captures/a.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in [
                {'off': 0, 'len': 1, 'dir': 'C2S', 'state': 'play'},
                {'off': 1, 'len': 1, 'dir': 'S2C', 'state': 'play'}]))
            atomic_json(root / 'captures/a.status.json', {'complete': True, 'bytes': 2})
            activity = evidence(); event = activity['evidence']['mechanics'][0]
            atomic_json(root / 'activity-result.json', activity)
            atomic_json(root / 'metadata.json', {'runId': 'test', 'assignment': '@activity organize_inventory',
                'identity': {'uuid': 'bot'}, 'mode': 'collection', 'versions': {'minecraft': '1.21.1'},
                'build': {'source': 'fixture'}, 'world': {'id': 'world', 'revision': 'one'}})
            health = {'valid': True, 'samples': 10, 'meanTps': 20, 'degradedWindows': 0}
            result = {'runId': 'test', 'diagnosticPassed': True, 'taskSuccess': True, 'datasetEligible': True,
                'capture': {'complete': True}, 'serverHealth': health, 'clientHealth': health}
            atomic_json(root / 'result.json', result)
            write_samples(root, [sample(99, 'bot', event['beforeSlots']), sample(102, 'bot', event['afterSlots'])])
            atomic_json(root / 'manifest.json', artifact_manifest(root, result))
            self.assertTrue(quality_report(root)['eligible'])
            result.update(diagnosticPassed=False, taskSuccess=False, datasetEligible=False)
            atomic_json(root / 'result.json', result); atomic_json(root / 'manifest.json', artifact_manifest(root, result))
            self.assertFalse(quality_report(root)['eligible'])
            result.update(diagnosticPassed=True, taskSuccess=True, datasetEligible=True)
            atomic_json(root / 'result.json', result)
            write_samples(root, [sample(99, 'bot', event['beforeSlots']), sample(102, 'other', event['afterSlots'])])
            atomic_json(root / 'manifest.json', artifact_manifest(root, result))
            self.assertIn('inventory_organize_server_unconfirmed', quality_report(root)['reasons'])

if __name__ == '__main__': unittest.main()
