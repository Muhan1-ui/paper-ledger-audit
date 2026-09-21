"""Read-only FotMob league-total xG summary for the current official card."""

import json
from concurrent.futures import ThreadPoolExecutor

from outputs.tai_cai_tracker.fotmob import build_match_xg_evidence, collect_team_stats, discover_team_stats_url


FIXTURES = {
    "2041150": ("Utrecht", "PSV Eindhoven"),
    "2041151": ("Jeju SK", "Daejeon Hana Citizen"),
    "2041152": ("Incheon United", "Jeonbuk Hyundai Motors"),
    "2041153": ("FC St. Pauli", "Kaiserslautern"),
    "2041154": ("AIK", "Hammarby"),
    "2041155": ("Feyenoord", "ADO Den Haag"),
    "2041156": ("Bodoe/Glimt", "Rosenborg"),
    "2041157": ("Sunderland", "Fulham"),
    "2041158": ("Leeds United", "Brentford"),
    "2041159": ("Paris FC", "Nice"),
    "2041160": ("Freiburg", "Werder Bremen"),
    "2041161": ("Telstar", "Ajax"),
    "2041162": ("Real Madrid", "Malaga"),
    "2041163": ("Manchester United", "Ipswich Town"),
    "2041164": ("Augsburg", "Schalke 04"),
}


def get_team(name):
    source = discover_team_stats_url(name)
    return collect_team_stats(source["source_url"])


def main():
    names = sorted({name for fixture in FIXTURES.values() for name in fixture})
    teams = {}
    failures = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {name: executor.submit(get_team, name) for name in names}
        for name, future in futures.items():
            try:
                teams[name] = future.result()
            except (OSError, ValueError, KeyError, TypeError) as error:
                failures[name] = str(error)

    output = {}
    for match_id, (home_name, away_name) in FIXTURES.items():
        if home_name in failures or away_name in failures:
            output[match_id] = {
                "status": "unavailable",
                "reason": {
                    name: failures[name]
                    for name in (home_name, away_name)
                    if name in failures
                },
            }
            continue
        try:
            evidence = build_match_xg_evidence(teams[home_name], teams[away_name])
            inputs = evidence["xg_inputs"]
            raw = inputs["evidence"]
            output[match_id] = {
                "status": "available",
                "source_url": inputs["source_url"],
                "competition": raw["competition"],
                "season": raw["season"],
                "sample_matches": raw.get("sample_matches"),
                "home_xg": inputs["home_xg"],
                "away_xg": inputs["away_xg"],
                "published_at": raw.get("published_at"),
                "scope": raw["scope"],
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            output[match_id] = {"status": "unavailable", "reason": str(error)}
    rendered = json.dumps(output, ensure_ascii=True, separators=(",", ":"))
    with open("work/today-xg.json", "w", encoding="utf-8") as output_file:
        output_file.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
