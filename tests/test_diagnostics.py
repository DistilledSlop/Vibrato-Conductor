import unittest
from conductor.diagnostics import Watchdog, task_success


def client(**changes):
    return {'seconds': 0, 'inventory': {}, 'blocksBrokenCount': 0, 'taskCompletedCount': 0,
            'dimension': 'minecraft:overworld', 'experienceLevel': 0, 'experienceProgress': 0,
            'rootClass': 'GetResourceTask', 'leafClass': 'MineTask', 'leaf': '<Mine ore>',
            'position': [0, 64, 0], 'chunk': [0, 0], 'onGround': True, 'tps': 20,
            **changes}


class DiagnosticTests(unittest.TestCase):
    def test_stationary_progress_is_not_stall_and_motion_is_not_progress(self):
        dog=Watchdog(stall_seconds=30)
        for second in range(60):
            self.assertIsNone(dog.observe(client(inventory={'minecraft:iron_ore': second}), None, {}, second))
        dog=Watchdog(stall_seconds=30)
        for second in range(31):
            assessment=dog.observe(client(position=[second,64,0]), None, {}, second)
        self.assertEqual(assessment['reason'], 'task_semantic_stall')
        self.assertEqual(assessment['confidence'], 'inferred')

    def test_exploration_novel_chunks_count_but_circles_do_not(self):
        dog=Watchdog(stall_seconds=30)
        for second in range(90):
            self.assertIsNone(dog.observe(client(leafClass='WanderTask', chunk=[second//10,0]), None, {}, second))
        dog=Watchdog(stall_seconds=600,loop_seconds=30)
        for second in range(40):
            assessment=dog.observe(client(position=[(second%4)*2,64,0]), None, {}, second)
        self.assertEqual(assessment['reason'], 'movement_loop_without_progress')

    def test_real_task_restart_counts_need_no_semantic_progress(self):
        starts=[{'seconds': 15, 'taskClass':'MineTask','task':'<Mine ore> retry'}]*8
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        result=dog.observe(client(seconds=15,recentTaskStarts=starts),None,{},15)
        self.assertEqual(result['reason'],'repeated_task_restart')
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        self.assertIsNone(dog.observe(client(seconds=15,recentTaskStarts=starts,leafClass='OtherTask'),None,{},15))
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        self.assertIsNone(dog.observe(client(seconds=15,recentTaskStarts=starts,inventory={'minecraft:coal':1}),None,{},15))

    def test_overload_precedes_behavior_classification(self):
        dog=Watchdog(stall_seconds=5)
        result=None
        for second in range(25):
            result=dog.observe(client(),{'ticks':second,'tps':10},{'cpuPressure': True},second)
        self.assertEqual(result['kind'],'overload')
        self.assertEqual(result['reason'],'server_tick_overload')

    def test_pillar_requires_repeated_attempts_and_grounded_no_height_gain(self):
        dog=Watchdog(pillar_seconds=10)
        for second in range(12):
            result=dog.observe(client(movement='MovementPillar',pillarAttempts=second),None,{},second)
        self.assertEqual(result['reason'],'nerd_pole_no_height_gain')
        dog=Watchdog(pillar_seconds=10)
        for second in range(12):
            self.assertIsNone(dog.observe(client(movement='MovementPillar',pillarAttempts=second,position=[0,64+second,0]),None,{},second))

    def test_client_overload_does_not_require_server_overload(self):
        dog=Watchdog()
        for second in range(25):
            result=dog.observe(client(seconds=second,tps=10),{'ticks':second,'tps':20},{},second)
        self.assertEqual(result['reason'],'client_tick_overload')

    def test_unexpected_idle_has_a_startup_and_settle_grace(self):
        dog=Watchdog()
        self.assertIsNone(dog.observe(client(rootTask=None,leafClass=None),None,{},0))
        self.assertIsNone(dog.observe(client(rootTask=None,leafClass=None),None,{},30))
        self.assertEqual(dog.observe(client(rootTask=None,leafClass=None),None,{},61)['reason'],'idle_without_active_task')

    def test_task_outcomes_need_success_evidence_and_goal(self):
        script={'status':'exit','commands':[{'action':'@nether','status':'completed','seconds':2}],
                'taskOutcomes':[{'seconds':3,'status':'completed'}],'finalDimension':'minecraft:the_nether'}
        self.assertTrue(task_success('@nether',script))
        script['finalDimension']='minecraft:overworld'
        self.assertFalse(task_success('@nether',script))
        self.assertIsNone(task_success('director',script))
        self.assertIsNone(task_success('@get oak_log 1',{}))
        script['commands'][0]['status']='timeout'
        self.assertFalse(task_success('@nether',script))


if __name__ == '__main__': unittest.main()
