import unittest
from conductor.diagnostics import IntervalRate, Watchdog, task_success, script_failure_reason, native_thread_failure


def client(**changes):
    return {'seconds': 0, 'inventory': {}, 'blocksBrokenCount': 0, 'taskCompletedCount': 0,
            'dimension': 'minecraft:overworld', 'experienceLevel': 0, 'experienceProgress': 0,
            'rootClass': 'GetResourceTask', 'leafClass': 'MineTask', 'leaf': '<Mine ore>',
            'position': [0, 64, 0], 'chunk': [0, 0], 'onGround': True, 'tps': 20,
            **changes}


class DiagnosticTests(unittest.TestCase):
    def test_native_thread_failure_is_confirmed_infrastructure_before_tick_overload(self):
        for text in ['java.lang.OutOfMemoryError: unable to create native thread: resource limits reached',
                     '[warning][os,thread] pthread_create failed (EAGAIN) for attributes: stacksize:1024k']:
            proof=native_thread_failure(text)
            self.assertIsNotNone(proof)
            result=Watchdog().observe(None,None,{'nativeThreadFailure':proof,'pidsCurrent':512,'pidsMax':512,'pidsLimitEvents':3},0)
            self.assertEqual(result['reason'],'worker_native_thread_exhausted')
            self.assertEqual(result['kind'],'infrastructure')
            self.assertEqual(result['confidence'],'confirmed')
            self.assertEqual(result['evidence']['pidsLimitEvents'],3)
        for text in ['java.lang.OutOfMemoryError: Java heap space', 'Failed to fetch user properties',
                     'pthread_create succeeded', 'java.net.UnknownHostException: sessionserver.mojang.com']:
            self.assertIsNone(native_thread_failure(text))
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        result=dog.observe(client(rollingTps=8,rollingSamples=10),None,{'nativeThreadFailure':proof},30)
        self.assertEqual(result['reason'],'worker_native_thread_exhausted')

    def test_weighted_cumulative_rates_do_not_alias_skipped_one_second_windows(self):
        rate=IntervalRate(window=2)
        self.assertIsNone(rate.update(0,0))
        self.assertEqual(rate.update(20,1),20)
        self.assertEqual(rate.update(60,4),40/3)
        self.assertEqual(rate.rolling,15)
        self.assertEqual(rate.average,15)
        self.assertEqual(rate.samples,2)
        rate.update(60,4)
        self.assertEqual(rate.samples,2)
        self.assertIsNone(rate.update(1,5))
        self.assertIsNone(rate.rolling)


    def test_weighted_window_overrides_biased_raw_snapshot_rates(self):
        dog=Watchdog()
        for second in range(25):
            self.assertIsNone(dog.observe(client(seconds=second,tps=8,rollingTps=19,rollingSamples=10),
                {'ticks':second,'tps':8,'rollingTps':20,'rollingSamples':10},{},second))
        self.assertEqual(dog.observe(client(seconds=26,tps=20,rollingTps=16,rollingSamples=10),
                {'ticks':26,'tps':20,'rollingTps':20,'rollingSamples':10},{},26)['reason'],'client_tick_overload')

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
        starts=[{'seconds': 15, 'taskClass':'MoveItemToSlotFromInventoryTask','task':'<Moving planks to slot 4> retry'}]*8
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        result=dog.observe(client(seconds=15,recentTaskStarts=starts,leafClass='MoveItemToSlotFromInventoryTask',leaf='<Moving planks to slot 4>'),None,{},15)
        self.assertEqual(result['reason'],'repeated_task_restart')
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        self.assertIsNone(dog.observe(client(seconds=15,recentTaskStarts=starts,leafClass='OtherTask'),None,{},15))
        dog=Watchdog()
        dog.observe(client(),None,{},0)
        self.assertIsNone(dog.observe(client(seconds=15,recentTaskStarts=starts,inventory={'minecraft:coal':1},leafClass='MoveItemToSlotFromInventoryTask',leaf='<Moving planks to slot 4>'),None,{},15))

    def test_moving_entity_navigation_churn_is_not_fast_failure(self):
        dog=Watchdog()
        for second in range(40):
            starts=[{'seconds':second,'taskClass':'GetToEntityTask','task':'<Approach pig>'}]*9
            self.assertIsNone(dog.observe(client(seconds=second,recentTaskStarts=starts,
                leafClass='GetToEntityTask',leaf='<Approach pig>',position=[second,64,0]),None,{},second))

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

    def test_bounded_successful_settle_skips_behavior_but_retains_overload(self):
        proof = dict(rootTask=None, rootClass=None, leafClass=None, scriptPhase='post_task_settle',
                     assignedTask='@get dirt 1', assignedTaskStatus='completed', settleBudgetSeconds=180,
                     settleSource='completed_command_idle_barrier_pending_exit')
        dog=Watchdog(stall_seconds=30, assigned_task='@get dirt 1', settle_seconds=180)
        for second in range(180):
            self.assertIsNone(dog.observe(client(seconds=second,settleRemainingSeconds=180-second,**proof),None,{},second))
        overloaded=client(seconds=181,tps=10,rollingTps=10,rollingSamples=10,settleRemainingSeconds=1,**proof)
        self.assertEqual(dog.observe(overloaded,None,{},181)['reason'],'client_tick_overload')
        dog=Watchdog(assigned_task='@get dirt 1',settle_seconds=180)
        self.assertEqual(dog.observe(client(settleRemainingSeconds=180,**proof),None,
                         {'memoryPressure':True,'oomKills':1},0)['reason'],'worker_memory_exhausted')

    def test_settle_requires_exact_assignment_success_and_deadline(self):
        proof=dict(rootTask=None,rootClass=None,leafClass=None,scriptPhase='post_task_settle',
                   assignedTask='@get dirt 1',assignedTaskStatus='completed',settleBudgetSeconds=120,
                   settleRemainingSeconds=120,settleSource='completed_command_idle_barrier_pending_exit')
        for changes, assignment in [({},'director'), ({'assignedTaskStatus':'dispatched'},'@get dirt 1'),
                ({'assignedTask':'@get stone 1'},'@get dirt 1'), ({'settleBudgetSeconds':180},'@get dirt 1'),
                ({'scriptPhase':'executing'},'@get dirt 1')]:
            dog=Watchdog(assigned_task=assignment,settle_seconds=120)
            dog.observe(client(**{**proof,**changes}),None,{},0)
            self.assertEqual(dog.observe(client(**{**proof,**changes}),None,{},61)['reason'],'idle_without_active_task')
        dog=Watchdog(assigned_task='@get dirt 1',settle_seconds=120)
        dog.observe(client(**proof),None,{},0)
        self.assertIsNone(dog.observe(client(**proof),None,{},123))
        self.assertEqual(dog.observe(client(**proof),None,{},184)['reason'],'idle_without_active_task')

    def test_script_timeout_cancel_and_error_keep_distinct_causes(self):
        self.assertEqual(script_failure_reason('@nether', {'commands':[{'status':'timeout'}]}),'task_budget_exceeded')
        self.assertEqual(script_failure_reason('@nether', {'commands':[{'status':'error'}]}),'task_command_error')
        self.assertEqual(script_failure_reason('@nether', {'taskOutcomes':[{'seconds':2,'status':'cancelled'}]}),'task_cancelled')
        self.assertEqual(script_failure_reason('@nether', {}),'task_success_unconfirmed')
        self.assertIsNone(script_failure_reason('director', {}))

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


class FocusedEvidenceTests(unittest.TestCase):
    def test_goal_requires_scoped_action_proof_and_valid_time_interval(self):
        from conductor.diagnostics import focused_activity_confirmed
        event={'id':'smelt','actionObserved':True,'goalTaskFinished':True,'success':True,
               'source':'client_action_and_task_outcome','packetDeltas':{'SERVERBOUND:container_click':2}}
        activity={'activity':'smelt_glass','source':'focused_activity_task','status':'completed',
                  'goalStartedWall':100,'goalFinishedWall':110,'evidence':{'mechanics':[event]}}
        self.assertTrue(focused_activity_confirmed('@activity smelt_glass', activity))
        for invalid in ({**activity,'activity':'fish'}, {**activity,'goalFinishedWall':99},
                        {**activity,'evidence':{'mechanics':[]}},
                        {**activity,'evidence':{'mechanics':[{**event,'packetDeltas':{}}]}},
                        {**activity,'evidence':{'mechanics':[{**event,'goalTaskFinished':False}]}}):
            self.assertFalse(focused_activity_confirmed('@activity smelt_glass', invalid))
