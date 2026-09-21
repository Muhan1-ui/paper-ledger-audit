"""Read-only collection summary for the current official daily card."""

import json
from concurrent.futures import ThreadPoolExecutor

from outputs.tai_cai_tracker.football433 import collect_433
from outputs.tai_cai_tracker.sporttery import collect_fixed_bonus


PAIRS = {
    "2041150": "1332254",
    "2041151": "1332182",
    "2041152": "1332291",
    "2041153": "1332183",
    "2041154": "1332184",
    "2041155": "1332332",
    "2041156": "1332185",
    "2041157": "1332186",
    "2041158": "1332148",
    "2041159": "1332149",
    "2041160": "1332153",
    "2041161": "1332301",
    "2041162": "1332302",
    "2041163": "1332159",
    "2041164": "1332343",
}


def collect_odds(match_id):
    return collect_433(f"https://www.433.cool/zqzhishu/data/{PAIRS[match_id]}.html")


def main():
    match_ids = list(PAIRS)
    with ThreadPoolExecutor(max_workers=4) as executor:
        official = dict(zip(match_ids, executor.map(collect_fixed_bonus, match_ids)))
    with ThreadPoolExecutor(max_workers=2) as executor:
        odds = dict(zip(match_ids, executor.map(collect_odds, match_ids)))

    summary = {}
    for match_id in match_ids:
        official_record = official[match_id]
        odds_record = odds[match_id]
        rows = odds_record.get("market", {}).get("rows", [])
        bookmaker = next(
            (row for row in rows if row.get("bookmaker") == "Bet 365"),
            rows[-1] if rows else {},
        )
        summary[match_id] = {
            "official_capture": official_record.get("captured_at"),
            "official_markets": {
                name: {
                    "status": value.get("status"),
                    "published_at": value.get("published_at"),
                    "option_count": len(value.get("fixed_prizes", {})),
                }
                for name, value in official_record.get("official_markets", {}).items()
            },
            "odds_capture": odds_record.get("captured_at"),
            "odds_url": odds_record.get("source_url"),
            "bookmaker": bookmaker.get("bookmaker"),
            "odds": bookmaker.get("prices"),
        }
    rendered = json.dumps(summary, ensure_ascii=True, separators=(",", ":"))
    with open("work/today-evidence.json", "w", encoding="utf-8") as output:
        output.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
