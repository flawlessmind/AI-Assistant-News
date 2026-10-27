#!/usr/bin/env python3
"""Small, explicit retrieval checks on the provided corpus, not a quality benchmark."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys

from alfa_research import ROOT, read_json, retrieve, verify_quote, write_json


def main():
    database = ROOT / "data/research.sqlite"
    as_of = date(2026, 10, 20)
    checks = []
    for query, expected_source, expected_page, options in [
        ("ключевая ставка пауза кредиты", "alfa_policy_rate", 1, {}),
        ("розничная торговля потребление спрос", "alfa_labor_7m", 1, {}),
        ("инфляция бензин недельная", "alfa_inflation", 1, {}),
        ("прогноз роста ВВП инвестиции", "alfa_gdp_1h", None, {}),
        ("золото ФРС", "alfa_gold", None, {"include_undated": True}),
        ("сталь прокат", "alfa_steel", None, {"include_historical": True}),
    ]:
        rows = retrieve(database, query, as_of, limit=5, **options)
        passed = any(r["source_id"] == expected_source and (expected_page is None or r["page"] == expected_page) for r in rows)
        checks.append({"check": "source_retrieved", "query": query, "expected_source": expected_source,
                       "expected_page": expected_page, "options": options, "passed": passed,
                       "returned": [{"source_id": r["source_id"], "page": r["page"]} for r in rows]})
    dated_rows = retrieve(database, "ключевая ставка пауза", date(2026, 9, 10), limit=100)
    checks.append({"check": "no_future_sources", "passed": all(r["publication_date"] <= "2026-09-10" for r in dated_rows)})
    old_rows = retrieve(database, "сталь прокат", as_of, limit=100)
    checks.append({"check": "steel_2021_excluded_by_default", "passed": all(r["source_id"] != "alfa_steel" for r in old_rows)})
    gold_rows = retrieve(database, "золото ФРС", as_of, limit=100)
    checks.append({"check": "unknown_publication_date_excluded_by_default", "passed": all(r["source_id"] != "alfa_gold" for r in gold_rows)})
    checks.append({"check": "unknown_query_returns_empty", "passed": retrieve(database, "совсемнесуществующееслово", as_of) == []})
    checks.append({"check": "altered_rate_quote_rejected", "passed": not verify_quote(database, "alfa_policy_rate", 1,
                   "Мы ожидаем сохранения паузы на заседании 23 октября, ставка 13,0%.")["quote_present_in_extracted_text"]})
    connection = sqlite3.connect(database)
    inventory = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                 for table in ["documents", "pages", "chunks"]}
    inventory["searchable_chunks"] = connection.execute("SELECT count(*) FROM chunks WHERE searchable=1").fetchone()[0]
    connection.close()
    packs = [read_json(ROOT / f"data/evidence_pack_{uid}.json") for uid in ["U_1595", "U_83932"]]
    checks.append({"check": "different_company_features_change_retrieval_queries",
                   "passed": [x["query"] for x in packs[0]["trace"]] != [x["query"] for x in packs[1]["trace"]]})
    report = {"as_of": str(as_of), "inventory": inventory, "checks": checks,
              "passed": sum(x["passed"] for x in checks), "total": len(checks),
              "llm_calls": 0, "api_cost_rub": 0,
              "limits": "These selected checks are not a representative accuracy estimate; numeric OCR and semantic entailment are not automatically validated."}
    write_json(ROOT / "data/quality_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(x["passed"] for x in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
