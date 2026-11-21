import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
sys.path.insert(0, ".")
from outputs.tai_cai_tracker.cli import _fotmob_search_name
from outputs.tai_cai_tracker.resolver import adopted_source_aliases

REG = "outputs/tai_cai_tracker/data/source_link_registry.json"
ALI = "outputs/tai_cai_tracker/data/source_aliases.json"

payload = json.load(open(ALI, encoding="utf-8"))
bak = json.load(open(ALI + ".bak-20261111", encoding="utf-8"))
print("[a] json.load ok; version =", payload["version"])
print("[a] aliases before =", len(bak["aliases"]), "after =", len(payload["aliases"]))
print("[a] new entries =", sum(
    1 for v in payload["aliases"].values()
    if v.get("confirmation_method") == "fotmob_search_verified"))
print("[a] all new keys normalized =", all(
    k == re.sub(r"\s+", "", k).casefold() for k in payload["aliases"]))
print("[a] new entries missing source_name/id =", [
    k for k, v in payload["aliases"].items()
    if v.get("confirmation_method") == "fotmob_search_verified"
    and not (isinstance(v.get("source_name"), str) and v["source_name"].strip()
             and v.get("source_team_id") is not None)])
print("[a] existing 10 entries preserved =",
      all(payload["aliases"][k] == v for k, v in bak["aliases"].items()))
print("[a] manual entries count =", sum(
    1 for v in payload["aliases"].values()
    if v.get("confirmation_method") == "manual_bilingual_review"))

al = adopted_source_aliases(ALI)
print("[b] adopted_source_aliases entries =", len(al))
for t in ["拜仁", "川崎前锋", "切尔西", "赫根", "皇家马德里", "AC米兰", "大宫松鼠"]:
    print("[b]", t, "->", _fotmob_search_name(t, al, REG))
