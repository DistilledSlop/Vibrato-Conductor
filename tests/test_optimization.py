import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
import zipfile

spec = importlib.util.spec_from_file_location('optimization_eval', Path(__file__).parents[1] / 'scripts/optimization-eval.py')
optimization = importlib.util.module_from_spec(spec)
spec.loader.exec_module(optimization)


class OptimizationTests(unittest.TestCase):
    def test_pinned_releases_match_game_loader_and_have_primary_hashes(self):
        value = optimization.load_profiles()
        self.assertEqual(value['policy']['defaultProfile'], 'baseline')
        self.assertFalse(value['policy']['experimentalCollectionAllowed'])
        self.assertEqual(value['profiles']['baseline']['mods'], [])
        for mod in value['mods'].values():
            self.assertIn('1.21.1', mod['minecraftVersions'])
            self.assertEqual(len(mod['sha512']), 128)
            self.assertEqual(len(mod['sha256']), 64)

    def test_download_cache_is_verified_and_corruption_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as jar:
                jar.writestr('fabric.mod.json', json.dumps({'id': 'testmod', 'version': '1.0'}))
            data = buffer.getvalue()
            path = root / 'test.jar'; path.write_bytes(data)
            mod = {'filename':path.name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                   'sha512':hashlib.sha512(data).hexdigest(),'modId':'testmod','version':'1.0'}
            self.assertEqual(optimization.verified_mod(mod, root), path)
            path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'integrity'): optimization.verified_mod(mod, root)

    def test_staging_baseline_does_not_enable_mods(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = optimization.stage_profile('baseline', root / 'stage', root / 'cache')
            self.assertFalse(value['experimental'])
            self.assertEqual(value['mods'], [])
            self.assertEqual(list((root / 'stage/mods').iterdir()), [])

    def test_comparison_never_certifies_equivalence_from_metrics(self):
        context = {'scenario':'survival_resource','assignment':'@get dirt 1'}
        run = {'runId':'baseline','context':context,'metrics':{'serverMeanTps':20,'wholeWorkerCpuSeconds':100},
               'observations':{'items':['minecraft:dirt']},'diagnosticPassed':True,'taskSuccess':True,
               'serverHealth':{'valid':True},'clientHealth':{'valid':True},'optimizationMods':[]}
        candidate = {**run,'runId':'candidate','optimizationMods':[{'modId':'ferritecore'}],
                     'metrics':{'serverMeanTps':20,'wholeWorkerCpuSeconds':50}}
        report = optimization.comparison_report([run],[candidate])
        self.assertFalse(report['behaviorEquivalent'])
        self.assertFalse(report['trainingEligible'])
        self.assertEqual(report['behaviorValidation'],'unvalidated')
        self.assertIn('mob_ai',report['missingBehaviorScenarios'])
        self.assertEqual(report['comparisons'][0]['metrics']['wholeWorkerCpuSeconds']['percent'],-50)


if __name__ == '__main__': unittest.main()
