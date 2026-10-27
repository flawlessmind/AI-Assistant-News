"""A corpus update must never replace existing data or accept unverified PDF files."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]


class BankUpdateTests(unittest.TestCase):
    def invoke(self,*args):
        return subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/update_bank.py'),*map(str,args)],
                              capture_output=True,encoding='utf-8',cwd=ROOT,timeout=30)

    def test_existing_directory_and_content_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'active';destination.mkdir()
            marker=destination/'research.sqlite';marker.write_bytes(b'protected-existing-index')
            result=self.invoke('--output-dir',destination)
            self.assertEqual(result.returncode,2)
            self.assertEqual(marker.read_bytes(),b'protected-existing-index')

    def test_missing_original_pdf_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'new'
            result=self.invoke('--output-dir',destination,'--pdf-dir',folder)
            self.assertEqual(result.returncode,2)
            self.assertIn('PDF hash mismatch',result.stderr)
            self.assertFalse(destination.exists())

    def test_manifest_cannot_fake_a_sha256(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest=json.loads((ROOT/'data/research_manifest.json').read_text('utf-8'))
            manifest[0]['sha256']='not-a-verified-hash'
            path=Path(folder)/'manifest.json';path.write_text(json.dumps(manifest),encoding='utf-8')
            destination=Path(folder)/'new'
            result=self.invoke('--manifest',path,'--output-dir',destination)
            self.assertEqual(result.returncode,2)
            self.assertFalse(destination.exists())
