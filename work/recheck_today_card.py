"""Read-only full-card evidence refresh from the current official schedule."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from outputs.tai_cai_tracker.football433 import collect_433
from outputs.tai_cai_tracker.fotmob import (
    build_match_xg_evidence,
    collect_team_stats,
    discover_team_stats_url,
)
from outputs.tai_cai_tracker.pregame import (
    build_pregame_bundle,
    build_xg_unavailable_bundle,
)
from outputs.tai_cai_tracker.sporttery import collect_fixed_bonus, collect_schedule


# These detail URLs were adopted only after the earlier strict 433 list match
# (official teams and kickoff within one minute).  The current rolling list may
# remove a fixture before kickoff, so it cannot be the sole lookup source.
SOURCES = {
    "2041138": ("1333329", "Viking", "Aalesund"),
    "2041150": ("1332254", "Utrecht", "PSV Eindhoven"),
    "2041151": ("1332182", "Jeju SK", "Daejeon Hana Citizen"),
    "2041152": ("1332291", "Incheon United", "Jeonbuk Hyundai Motors"),
    "2041153": ("1332183", "FC St. Pauli", "Kaiserslautern"),
    "2041154": ("1332184", "AIK", "Hammarby"),
    "2041155": ("1332332", "Feyenoord", "ADO Den Haag"),
    "2041156": ("1332185", "Bodoe/Glimt", "Rosenborg"),
    "2041157": ("1332186", "Sunderland", "Fulham"),
    "2041158": ("1332148", "Leeds United", "Brentford"),
    "2041159": ("1332149", "Paris FC", "Nice"),
    "2041160": ("1332153", "Freiburg", "Werder Bremen"),
    "2041161": ("1332301", "Telstar", "Ajax"),
    "2041162": ("1332302", "Real Madrid", "Malaga"),
    "2041163": ("1332159", "Manchester United", "Ipswich Town"),
    "2041164": ("1332343", "Augsburg", "Schalke 04"),
}


def summarize(match, bundle):
    evidence = bundle["evidence"]
    official = evidence["official"]
    odds = evidence["european_1x2"]
    xg = evidence["xg"]
    xg_inputs = xg.get("xg_inputs", {})
    xg_evidence = xg_inputs.get("evidence", {})
    captures = [
        item.get("captured_at") for item in xg.get("stat_observations", [])
        if isinstance(item, dict)
    ]
    return {
        "match_id": str(match["match_id"]),
        "competition": match["competition"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "kickoff_at": match["kickoff_at"],
        "official_source_url": match["official_source_url"],
        "official_markets": {
            name: {
                "status": value.get("status"),
                "published_at": value.get("published_at"),
                "option_count": len(value.get("fixed_prizes", {})),
            }
            for name, value in official.get("official_markets", {}).items()
        },
        "european_1x2": {
            "source_url": odds.get("source_url"),
            "captured_at": odds.get("captured_at"),
            "provider": odds.get("provider"),
            "rows": odds.get("market", {}).get("rows", []),
        },
        "xg": {
            "status": "available" if xg_inputs else "unavailable",
            "source_url": xg_evidence.get("source_url"),
            "captured_at": max(captures) if captures else None,
            "competition": xg_evidence.get("competition"),
            "season": xg_evidence.get("season"),
            "scope": xg_evidence.get("scope"),
            "sample_matches": xg_evidence.get("sample_matches"),
            "home_sample_matches": xg_evidence.get("home_sample_matches"),
            "away_sample_matches": xg_evidence.get("away_sample_matches"),
        },
        "quality_gate": bundle.get("quality_gate"),
        "readiness": bundle.get("readiness"),
    }


def main():
    schedule = collect_schedule(method="concern")
    today = date.today().isoformat()
    matches = [
        match for match in schedule["matches"] if match["kickoff_at"].startswith(today)
    ]
    results = {}
    source_matches = [match for match in matches if str(match["match_id"]) in SOURCES]
    missing_sources = [match for match in matches if str(match["match_id"]) not in SOURCES]
    official = {}
    odds = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(collect_fixed_bonus, match["match_id"]): match for match in source_matches}
        for future in as_completed(futures):
            match = futures[future]
            try:
                official[str(match["match_id"])] = future.result()
            except (OSError, ValueError, KeyError, TypeError) as error:
                results[str(match["match_id"])] = {
                    "match_id": str(match["match_id"]),
                    "competition": match["competition"],
                    "home_team": match["home_team"],
                    "away_team": match["away_team"],
                    "kickoff_at": match["kickoff_at"],
                    "official_source_url": match["official_source_url"],
                    "status": "unavailable",
                    "reason": str(error),
                }
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(collect_433, f"https://www.433.cool/zqzhishu/data/{SOURCES[str(match['match_id'])][0]}.html"): match
            for match in source_matches if str(match["match_id"]) in official
        }
        for future in as_completed(futures):
            match = futures[future]
            try:
                odds[str(match["match_id"])] = future.result()
            except (OSError, ValueError, KeyError, TypeError) as error:
                results[str(match["match_id"])] = {
                    "match_id": str(match["match_id"]), "status": "unavailable", "reason": str(error)
                }

    team_names = sorted({name for match in source_matches for name in SOURCES[str(match["match_id"])][1:]})
    teams = {}
    team_failures = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(lambda name: collect_team_stats(discover_team_stats_url(name)["source_url"]), name): name
            for name in team_names
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                teams[name] = future.result()
            except (OSError, ValueError, KeyError, TypeError) as error:
                team_failures[name] = str(error)

    for match in source_matches:
        match_id = str(match["match_id"])
        if match_id in results or match_id not in official or match_id not in odds:
            continue
        _, home_name, away_name = SOURCES[match_id]
        try:
            xg = build_match_xg_evidence(teams[home_name], teams[away_name])
            bundle = build_pregame_bundle(match, official[match_id], odds[match_id], xg)
        except (OSError, ValueError, KeyError, TypeError) as error:
            bundle = build_xg_unavailable_bundle(
                match,
                official[match_id],
                odds[match_id],
                {},
                {"status": "skipped", "reason": f"xg_unavailable: {error}"},
            )
        results[match_id] = summarize(match, bundle)

    for match in missing_sources:
        results[str(match["match_id"])] = {
            "match_id": str(match["match_id"]),
            "competition": match["competition"],
            "home_team": match["home_team"],
            "away_team": match["away_team"],
            "kickoff_at": match["kickoff_at"],
            "official_source_url": match["official_source_url"],
            "status": "unavailable",
            "reason": "no previously verified 433 detail URL",
        }
    output = {
        "schedule_source_url": schedule["source_url"],
        "card_date": today,
        "match_count": len(matches),
        "matches": results,
    }
    with open("work/today-recheck.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
