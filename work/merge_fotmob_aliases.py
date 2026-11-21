"""One-off: merge FotMob search-verified team aliases into source_aliases.json."""
import json
import re
import sys
from datetime import datetime

INPUT_PATH, TARGET_PATH = sys.argv[1], sys.argv[2]

with open(TARGET_PATH, "rb") as handle:
    raw = handle.read()
payload = json.loads(raw.decode("utf-8"))
version_before = payload.get("version")
aliases = payload["aliases"]
before = len(aliases)

with open(INPUT_PATH, encoding="utf-8") as handle:
    source = json.load(handle)

verified_at = datetime.now().astimezone().isoformat()
added, skipped = [], []
for raw_key, item in source.items():
    team_name = item.get("team_name") or raw_key
    key = re.sub(r"\s+", "", team_name).casefold()
    if key in aliases:
        skipped.append(key)
        continue
    if not (isinstance(item.get("source_name"), str) and item["source_name"].strip()):
        raise SystemExit(f"bad source_name for {team_name!r}")
    if item.get("source_team_id") is None:
        raise SystemExit(f"bad source_team_id for {team_name!r}")
    aliases[key] = {
        "confirmation_method": "fotmob_search_verified",
        "first_seen_at": verified_at,
        "score": item["score"],
        "search_url": item["search_url"],
        "source_name": item["source_name"],
        "source_team_id": item["source_team_id"],
        "source_url": item["source_url"],
        "verified_at": verified_at,
    }
    added.append(key)

if payload.get("version") != version_before:
    raise SystemExit("version changed unexpectedly")

serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
with open(TARGET_PATH, "wb") as handle:
    handle.write(serialized.encode("utf-8") + b"\r\n")

print(f"aliases before={before} input={len(source)} added={len(added)} "
      f"skipped_existing={len(skipped)} after={len(aliases)}")
print("verified_at:", verified_at)
print("skipped existing keys:", skipped)
