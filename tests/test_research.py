"""Offline checks for evidence integrity and date leakage, using synthetic fixtures."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alfa_research import build_index, open_database, retrieve, sha256, verify_quote, write_json


class EvidenceSafetyTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("ALFA_TEST_TMP", str(Path(__file__).resolve().parents[1] / ".cache/tests"))).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        if not self.root.resolve().is_relative_to(scratch):
            raise ValueError("Test directory is outside the intended scratch directory")
        self.manifest = self.root / "manifest.json"
        self.pages_path = self.root / "pages.jsonl"
        self.database = self.root / "research.sqlite"
        entries = []
        self.pages = []
        for source_id, published, excluded in [
            ("fresh", "2026-09-11", []), ("old", "2021-04-15", []),
            ("future", "2026-11-01", []), ("undated", None, []),
            ("legal", "2026-09-11", [1]),
        ]:
            entries.append({"source_id": source_id, "file_name": source_id + ".pdf",
                            "publication_date": published, "date_review_status": "visual_verified",
                            "source_url": "https://example.invalid/" + source_id,
                            "exclude_from_search_pages": excluded, "page_count": 1})
            text = "Синтетический тест. Ключевая ставка 14%. Кредитные продукты.\n" * 45
            self.pages.append({"source_id": source_id, "page": 1, "text": text,
                               "text_sha256": sha256(text.encode()),
                               "pdf_sha256": sha256(source_id.encode()),
                               "extraction_method": "synthetic_test_fixture"})
        write_json(self.manifest, entries)
        self.save_pages()
        build_index(self.manifest, self.pages_path, self.database)

    def save_pages(self):
        self.pages_path.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in self.pages), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_fresh_results_exclude_future_unknown_dates_and_legal_pages(self):
        rows = retrieve(self.database, "ключевая ставка кредиты", date(2026, 10, 20))
        self.assertEqual([x["source_id"] for x in rows], ["fresh"])

    def test_historical_requires_explicit_opt_in_and_never_allows_future(self):
        rows = retrieve(self.database, "ставка", date(2026, 10, 20), include_historical=True)
        self.assertEqual({x["source_id"] for x in rows}, {"fresh", "old"})
        self.assertTrue(next(x for x in rows if x["source_id"] == "old")["historical_for_query"])

    def test_unknown_date_requires_explicit_opt_in_and_warning(self):
        rows = retrieve(self.database, "ставка", date(2026, 10, 20), include_undated=True)
        undated = next(row for row in rows if row["source_id"] == "undated")
        self.assertIsNone(undated["publication_date"])
        self.assertTrue(undated["publication_date_unknown"])
        self.assertIsNone(undated["age_days"])
        self.assertIn("availability as of query date is not established", undated["warning"])

    def test_event_date_prevents_looking_ahead(self):
        rows = retrieve(self.database, "кредиты", date(2021, 4, 16))
        self.assertEqual([x["source_id"] for x in rows], ["old"])

    def test_search_input_is_literal_and_empty_query_is_safe(self):
        self.assertEqual(retrieve(self.database, '" : () *', date(2026, 10, 20)), [])
        self.assertEqual(retrieve(self.database, "совсемнесуществующееслово", date(2026, 10, 20)), [])

    def test_each_returned_quote_maps_to_exact_page_offsets(self):
        row = retrieve(self.database, "ставке", date(2026, 10, 20))[0]
        original = next(p["text"] for p in self.pages if p["source_id"] == row["source_id"])
        self.assertEqual(original[row["char_start"]:row["char_end"]], row["text"])
        self.assertTrue(row["source_url"] in row["page_url"])

    def test_changed_number_is_rejected_and_quote_match_is_not_entailment(self):
        correct = verify_quote(self.database, "fresh", 1, "Ключевая ставка 14%.")
        self.assertTrue(correct["quote_present_in_extracted_text"])
        self.assertFalse(correct["semantic_entailment_checked"])
        self.assertFalse(verify_quote(self.database, "fresh", 1, "Ключевая ставка 13%.")["quote_present_in_extracted_text"])
        self.assertFalse(verify_quote(self.database, "fresh", 99, "Ключевая ставка 14%.")["quote_present_in_extracted_text"])

    def test_tampered_page_or_missing_page_fails_index_build(self):
        self.pages[0]["text"] += " Unreviewed edit"
        self.save_pages()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            build_index(self.manifest, self.pages_path, self.root / "bad.sqlite")
        self.pages.pop(0)
        self.save_pages()
        with self.assertRaisesRegex(ValueError, "inventories differ"):
            build_index(self.manifest, self.pages_path, self.root / "missing.sqlite")

    def test_rebuild_keeps_evidence_ids_stable(self):
        before = retrieve(self.database, "ставка", date(2026, 10, 20))[0]["evidence_id"]
        build_index(self.manifest, self.pages_path, self.database)
        after = retrieve(self.database, "ставка", date(2026, 10, 20))[0]["evidence_id"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
