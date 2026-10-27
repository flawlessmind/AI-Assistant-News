"""Release boundary checks: source provenance survives, private state does not."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('package_release', Path(__file__).resolve().parents[1]/'scripts/package_release.py')
package_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_release)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_release_keeps_source_snapshots_and_excludes_private_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            included = ['.env.example', 'Dockerfile', 'README.md', 'START_HERE_RU.md',
                        'alfa_app/server.py', 'web/app.js', 'data/research.sqlite',
                        'data/news_sources/feed.xml', 'data/news_sources/article.html']
            excluded = ['.env', '.env.local', 'var/service.sqlite', 'var/fix_backups/app.py',
                        '.agents/skills/test.py', '.git/config', 'node_modules/a.js',
                        'data/raw/input.pdf', 'data/companies/source.csv', 'data/private-config.json',
                        'docs/config.private.md', 'alfa_app/__pycache__/app.pyc',
                        'apply_alfa_llm_fix.py', 'old.zip']
            for name in included + excluded:
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            actual = {p.relative_to(root).as_posix() for p in package_release.release_files(root)}
            self.assertEqual(actual, set(included))
