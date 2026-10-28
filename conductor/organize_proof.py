"""Independent server inventory evidence for the focused inventory organizer."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path


def _time(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _layout_totals(value):
    if not isinstance(value, list) or len(value) != 41: return None
    totals = Counter()
    for entry in value:
        if not isinstance(entry, str) or not re.fullmatch(r'[a-z0-9_./-]+:(0|[1-9][0-9]{0,9})', entry): return None
        item, count_text = entry.rsplit(':', 1)
        count = int(count_text)
        if count > 2147483647 or (item == 'air') != (count == 0): return None
        if count: totals[item] += count
    return totals


def organize_server_confirmed(root: Path, activity: object, identity_uuid: object) -> bool:
    """Match goal-scoped client layout to snapshots from the server observer.

    Approximately one-second server samples are bounded to five seconds before goal start and
    ten seconds after goal completion; no packet echo is assumed for local slots.
    """
    if not isinstance(identity_uuid, str) or not identity_uuid or not isinstance(activity, dict): return False
    if (activity.get('activity') != 'organize_inventory' or activity.get('source') != 'focused_activity_task'
            or activity.get('status') != 'completed'): return False
    start, end = activity.get('goalStartedWall'), activity.get('goalFinishedWall')
    if not _time(start) or not _time(end) or start <= 0 or end < start: return False
    evidence = activity.get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('mechanics'), list): return False
    event = next((event for event in evidence['mechanics'] if isinstance(event, dict)
                  and event.get('id') == 'inventory_organize' and event.get('success') is True
                  and event.get('goalTaskFinished') is True and event.get('actionObserved') is True
                  and event.get('source') == 'client_action_and_task_outcome'), None)
    if event is None: return False
    before, after = event.get('beforeSlots'), event.get('afterSlots')
    before_totals, after_totals = _layout_totals(before), _layout_totals(after)
    if before_totals is None or after_totals is None or before_totals != after_totals or before == after: return False
    packets = event.get('packetDeltas')
    clicks = packets.get('SERVERBOUND:minecraft:container_click') if isinstance(packets, dict) else None
    if not isinstance(clicks, int) or isinstance(clicks, bool) or clicks <= 0: return False
    baseline, completed = False, False
    try:
        with (Path(root) / 'server-heartbeats.jsonl').open() as stream:
            for line in stream:
                sample = json.loads(line)
                if not isinstance(sample, dict) or sample.get('source') != 'fabric_end_server_tick': continue
                wall = sample.get('wall')
                if not _time(wall): continue
                snapshots = sample.get('playerSnapshots')
                if not isinstance(snapshots, list): continue
                for player in snapshots:
                    if not isinstance(player, dict) or player.get('uuid') != identity_uuid: continue
                    slots = player.get('inventorySlots')
                    if start - 5 <= wall <= start and slots == before: baseline = True
                    if end <= wall <= end + 10 and slots == after: completed = True
    except (OSError, ValueError, TypeError):
        return False
    return baseline and completed
