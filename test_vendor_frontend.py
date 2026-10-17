import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('vendor_frontend', Path(__file__).with_name('vendor_frontend.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@unittest.skipUnless(shutil.which('npm'), 'npm is required for caller-sequence validation')
class VendorTest(unittest.TestCase):
    def test_real_npm_lock_and_clean_install_from_reviewed_monorepo_edge(self):
        self.exercise('file:../../packages/shizuha-ui')

    def test_foreign_monorepo_edge_refuses_before_package_changes(self):
        for requested in ('file:../../packages/shizuha-chat', 'file:/packages/shizuha-ui', 'file:../../../shizuha-ui'):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                self.exercise(requested)

    def test_real_npm_lock_and_clean_install_from_local_artifact(self):
        self.exercise('^1.2.6')

    def exercise(self, requested):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / 'frontend'
            frontend.mkdir()
            artifacts = root / 'artifacts'
            artifacts.mkdir()
            metadata = {'name': '@shizuha/ui', 'version': '1.2.6', 'main': 'index.js'}
            with tarfile.open(artifacts / 'ui.tgz', 'w:gz') as archive:
                for filename, content in [('package/package.json', json.dumps(metadata)), ('package/index.js', 'module.exports = "fixture";')]:
                    content = content.encode()
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            package = {'name': 'fixture-frontend', 'version': '1.0.0', 'dependencies': {'@shizuha/ui': requested}}
            (frontend / 'package.json').write_text(json.dumps(package))
            (frontend / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3, 'packages': {'': package}}))
            module.vendor(frontend, artifacts)
            module.subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--offline'], cwd=frontend, check=True, capture_output=True)
            result = module.subprocess.check_output(['node', '-e', 'console.log(require("@shizuha/ui"))'], cwd=frontend, text=True)
            self.assertEqual(result.strip(), 'fixture')
            lock = json.loads((frontend / 'package-lock.json').read_text())
            self.assertEqual(lock['packages']['node_modules/@shizuha/ui']['resolved'], 'file:vendor/ui.tgz')
            # Repack the same dependency graph with a newly supplied license,
            # while node_modules from the previous installation still exists.
            with tarfile.open(artifacts / 'ui.tgz', 'w:gz') as archive:
                for filename, content in [('package/package.json', json.dumps(metadata)),
                                          ('package/index.js', 'module.exports = "fixture";'),
                                          ('package/LICENSE', 'Reviewed fixture license')]:
                    content = content.encode()
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            module.vendor(frontend, artifacts)
            module.subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--offline'],
                                  cwd=frontend, check=True, capture_output=True)
            self.assertEqual((frontend / 'node_modules/@shizuha/ui/LICENSE').read_text(),
                             'Reviewed fixture license')
            self.assertEqual(module.subprocess.check_output(
                ['node', '-e', 'console.log(require("@shizuha/ui"))'], cwd=frontend, text=True).strip(), 'fixture')
            # A later attempt missing that required artifact fails before writes.
            (artifacts / 'ui.tgz').unlink()
            previous = (frontend / 'package-lock.json').read_bytes()
            with self.assertRaises(ValueError):
                module.vendor(frontend, artifacts)
            self.assertEqual((frontend / 'package-lock.json').read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
