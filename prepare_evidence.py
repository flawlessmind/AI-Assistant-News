#!/usr/bin/env python3
"""Prepare a traceable context pack; this is deterministic retrieval, not an AI agent."""
import argparse
import json
from datetime import date
from pathlib import Path

from alfa_research import ROOT, read_json, retrieve, sha256, write_json


def prepare(profile, event, database: Path, as_of: date):
    event_date = date.fromisoformat(event["published_at"][:10])
    if event_date > as_of:
        raise ValueError("The event is in the future relative to analysis date")
    company = profile["company"]
    features = []
    queries = []
    topics = event.get("topics", [])
    if "key_rate" in topics:
        queries.extend(["ключевая ставка пауза прогноз", "ставка сохранения паузы конец года"])
    else:
        queries.append(event["title"])
    okved = company.get("industry_okved_code") or ""
    features.append({"path": "company.industry_okved_code", "value": okved})
    features.append({"path": "company.industry_name", "value": company.get("industry_name")})
    if okved.startswith("47"):
        queries.append("розничная торговля потребление спрос")
    elif okved.startswith("41"):
        queries.append("строительство инвестиции финансирование")
    if company.get("products", {}).get("credit") is True:
        queries.append("корпоративные кредиты ключевая ставка")
        features.append({"path": "company.products.credit", "value": True})
    if company.get("products", {}).get("leasing") is True:
        features.append({"path": "company.products.leasing", "value": True})
    evidence = {}
    trace = []
    for query in dict.fromkeys(queries):
        rows = retrieve(database, query, as_of, limit=3)
        for row in rows:
            evidence[row["evidence_id"]] = row
        trace.append({"tool": "retrieve_bank_research", "query": query,
                      "as_of": str(as_of), "returned_evidence_ids": [r["evidence_id"] for r in rows]})
    if not evidence:
        status = "insufficient_evidence"
    else:
        status = "ready_for_human_or_allowed_llm_analysis"
    return {"schema_version": 1, "stage": "deterministic_evidence_preparation",
            "status": status, "company_id": profile["company_id"], "analysis_as_of": str(as_of),
            "event": event, "profile_sha256": sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()),
            "company_features_used": features, "evidence": list(evidence.values()), "trace": trace,
            "limitations": ["Queries are explicit rules; there is no autonomous planning, generation or self-correction yet.",
                            "An evidence pack is not a finished insight. Sources still need semantic and numeric review.",
                            "Credit product flags do not reveal debt principal, interest rate or repricing dates.",
                            "No FTS flag is interpreted as proof of imports, exports or FX exposure."],
            "llm_calls": 0, "api_cost_rub": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--event", required=True, type=Path)
    parser.add_argument("--database", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(read_json(args.profile), read_json(args.event), args.database, args.as_of)
    write_json(args.output, result)
    print(json.dumps({"company_id": result["company_id"], "status": result["status"],
                      "evidence_count": len(result["evidence"]), "tool_calls": len(result["trace"]),
                      "llm_calls": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
