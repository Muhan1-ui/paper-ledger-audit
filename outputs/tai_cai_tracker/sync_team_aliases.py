"""Sync new official Chinese team names into the local FotMob alias registry.

Reads the public China Sports Lottery schedule, translates Chinese-only names
through the local LLM gateway, verifies each candidate through the existing
strict FotMob search resolver, and appends only high-confidence identities to
the mutable alias and source-link registries. It never touches the immutable
paper ledger and never writes a prediction, snapshot, or settlement.

Usage:
    python -m outputs.tai_cai_tracker.sync_team_aliases [--dry-run]

Paper-only operations tool. No purchase, bet placement, or funding action.
"""

import argparse
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .fotmob import FotMobSearchResolutionError, discover_team_stats_url


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "outputs" / "tai_cai_tracker" / "data"
ALIAS_PATH = DATA_DIR / "source_aliases.json"
REGISTRY_PATH = DATA_DIR / "source_link_registry.json"
PENDING_PATH = PROJECT_ROOT / "work" / "pending_team_aliases.json"

SCHEDULE_ENDPOINT = (
    "https://webapi.sporttery.cn/gateway/uniform/fb/getMatchDataPageListV1.qry"
)
SCHEDULE_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.sporttery.cn/",
}
SCHEDULE_PAGE_SIZE = 100
SCHEDULE_MAX_PAGES = 10
TEAM_NAME_FIELDS = (
    "homeTeamAbbName",
    "homeTeamAllName",
    "awayTeamAbbName",
    "awayTeamAllName",
)

LLM_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
LLM_MODEL = "workbuddy/deepseek-v4.1-flash"
LLM_HEADERS = {
    "Authorization": "Bearer magpie",
    "Content-Type": "application/json",
}
LLM_BATCH_SIZE = 30
LLM_PROMPT_PREFIX = (
    "把这些中文足球队名翻译成 FotMob 网站上使用的官方英文队名，"
    "只输出 JSON 对象，key 是中文原名，value 是英文名数组（最多2个候选，"
    "第一个最可能），不要解释。注意区分俱乐部和国家队："
    "国家队要译成国家英文名（如 英格兰->England、捷克->Czechia、芬兰->Finland）。"
    "队名："
)

FOTMOB_PROXY = "http://127.0.0.1:7892"
MINIMUM_SCORE = 0.95
NETWORK_RETRIES = 2
RETRY_INTERVAL_SECONDS = 2
CJK_PATTERN = re.compile(
    r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f]"
)


def _local_now():
    return datetime.now(timezone(timedelta(hours=8)))


def _team_key(name):
    """Match cli._fotmob_search_name's lookup key exactly."""
    return re.sub(r"\s+", "", name).casefold()


def _direct_opener():
    """Bypass every ambient proxy for domestic or local endpoints."""
    return build_opener(ProxyHandler({}))


def _is_network_error(error):
    if isinstance(error, HTTPError):
        return False
    return isinstance(
        error, (URLError, TimeoutError, socket.timeout, ConnectionError, OSError)
    )


def _with_retries(operation, label):
    """Retry only transport failures; HTTP status errors surface immediately."""
    last_error = None
    for attempt in range(NETWORK_RETRIES + 1):
        try:
            return operation()
        except HTTPError:
            raise
        except Exception as error:  # noqa: BLE001 - classified below
            if not _is_network_error(error):
                raise
            last_error = error
            if attempt < NETWORK_RETRIES:
                print(
                    f"[warn] {label} network error, retrying in "
                    f"{RETRY_INTERVAL_SECONDS}s: {error}",
                    file=sys.stderr,
                )
                time.sleep(RETRY_INTERVAL_SECONDS)
    raise last_error


def _read_json(path):
    """Read a JSON document and remember its exact trailing newline bytes."""
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    trailing = raw[len(body):] if raw.rstrip(b"\r\n") == body else b"\n"
    return payload, trailing


def _atomic_write_json(path, payload, trailing):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if trailing not in (b"\n", b"\r\n"):
        trailing = b"\n"
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(body + trailing)
    os.replace(tmp_path, path)


def _backup(path, date_stamp):
    backup_path = path.with_name(f"{path.name}.bak-{date_stamp}")
    if backup_path.exists():
        return backup_path, "exists"
    backup_path.write_bytes(path.read_bytes())
    return backup_path, "created"


def _schedule_page_url(page_no):
    query = urlencode(
        {
            "method": "concern",
            "clientCode": "3001",
            "pageSize": SCHEDULE_PAGE_SIZE,
            "pageNo": page_no,
        }
    )
    return f"{SCHEDULE_ENDPOINT}?{query}"


def _fetch_schedule_page(page_no):
    def load():
        request = Request(_schedule_page_url(page_no), headers=SCHEDULE_HEADERS)
        with _direct_opener().open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    return _with_retries(load, f"sporttery page {page_no}")


def collect_schedule_names():
    """Scan the official schedule and return (match_count, {key: name})."""
    matches = {}
    names = {}
    for page_no in range(1, SCHEDULE_MAX_PAGES + 1):
        payload = _fetch_schedule_page(page_no)
        if str(payload.get("errorCode")) != "0":
            raise RuntimeError(
                "sporttery schedule API error: "
                f"{payload.get('errorMessage') or payload.get('errorCode')}"
            )
        groups = payload.get("value", {}).get("matchInfoList", [])
        page_rows = [
            row
            for group in groups
            if isinstance(group, dict)
            for row in group.get("subMatchList", [])
            if isinstance(row, dict)
        ]
        if not page_rows:
            break
        new_matches = 0
        for row in page_rows:
            match_id = row.get("matchId")
            identity = str(match_id) if match_id is not None else None
            if identity is not None:
                if identity in matches:
                    continue
                matches[identity] = True
            new_matches += 1
            for field in TEAM_NAME_FIELDS:
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    names.setdefault(_team_key(name), name)
        # The public endpoint can repeat one page regardless of pageNo; stop
        # when a whole page contributes no new match identity.
        if new_matches == 0:
            break
    return len(matches), names


def _read_pending_names():
    try:
        payload = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pending = payload.get("pending") if isinstance(payload, dict) else None
    names = {}
    if isinstance(pending, list):
        for item in pending:
            if not isinstance(item, dict):
                continue
            name = item.get("zh")
            if isinstance(name, str) and name.strip():
                names.setdefault(_team_key(name.strip()), name.strip())
    return names


def _post_llm(body, counter):
    counter["llm_calls"] += 1
    request = Request(
        LLM_ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=LLM_HEADERS,
        method="POST",
    )
    with _direct_opener().open(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_translation(content, names):
    if not isinstance(content, str):
        return {}
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(content[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    translated = {}
    for name in names:
        values = data.get(name)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        candidates = []
        for value in values:
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if candidate not in candidates:
                    candidates.append(candidate)
        if candidates:
            translated[name] = candidates[:2]
    return translated


def _translate_batch(names, counter):
    """Return {chinese_name: [candidate, ...]}; empty on any LLM failure."""
    prompt = LLM_PROMPT_PREFIX + "、".join(names)
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    try:
        payload = _with_retries(
            lambda: _post_llm(body, counter), "llm translation"
        )
    except Exception as error:  # noqa: BLE001 - a batch failure is not fatal
        print(f"[warn] LLM batch failed: {error}", file=sys.stderr)
        return {}
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("[warn] LLM response has no message content", file=sys.stderr)
        return {}
    return _parse_translation(content, names)


def _try_candidate(candidate):
    """Search one English candidate; return (result, failure_reason)."""
    try:
        result = _with_retries(
            lambda: discover_team_stats_url(candidate), f"fotmob search {candidate!r}"
        )
    except FotMobSearchResolutionError as error:
        return None, error.failure_code or "no_match"
    except HTTPError as error:
        return None, f"http_{error.code}"
    except Exception as error:  # noqa: BLE001 - per-name failure stays isolated
        if _is_network_error(error):
            return None, "network_error"
        return None, "search_error"
    if not isinstance(result, dict):
        return None, "invalid_result"
    team_id = result.get("team_id")
    score = result.get("score")
    if team_id in (None, ""):
        return None, "missing_team_id"
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None, "missing_score"
    if score < MINIMUM_SCORE:
        return None, "score_below_threshold"
    return result, None


def _resolve_names(names, counter):
    """Translate Chinese names in bounded batches, then verify each candidate."""
    chinese = [name for name in names if CJK_PATTERN.search(name)]
    translated = {}
    for start in range(0, len(chinese), LLM_BATCH_SIZE):
        batch = chinese[start : start + LLM_BATCH_SIZE]
        translated.update(_translate_batch(batch, counter))
    results = {}
    for name in names:
        if CJK_PATTERN.search(name):
            candidates = translated.get(name, [])
            if not candidates:
                results[name] = (None, [], "llm_translation_failed")
                continue
            adopted = None
            for candidate in candidates:
                result, _reason = _try_candidate(candidate)
                if result is not None:
                    adopted = result
                    break
            results[name] = (
                (adopted, candidates, None)
                if adopted is not None
                else (None, candidates, "no_candidate_passed")
            )
        else:
            result, reason = _try_candidate(name)
            results[name] = (
                (result, [name], None) if result is not None else (None, [name], reason)
            )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync new schedule team names into the FotMob alias registry."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve names and print planned writes without touching any file",
    )
    args = parser.parse_args(argv)

    # FotMob is the only public source that needs the local proxy. The project
    # transport reads TAICAI_HTTP_PROXY first; set all three for compatibility.
    os.environ["TAICAI_HTTP_PROXY"] = FOTMOB_PROXY
    os.environ.setdefault("HTTP_PROXY", FOTMOB_PROXY)
    os.environ.setdefault("HTTPS_PROXY", FOTMOB_PROXY)
    os.environ.setdefault("http_proxy", FOTMOB_PROXY)
    os.environ.setdefault("https_proxy", FOTMOB_PROXY)

    for path in (ALIAS_PATH, REGISTRY_PATH):
        if not path.is_file():
            print(f"[error] missing required file: {path}", file=sys.stderr)
            return 1

    alias_payload, alias_trailing = _read_json(ALIAS_PATH)
    registry_payload, registry_trailing = _read_json(REGISTRY_PATH)
    aliases = alias_payload.get("aliases")
    registry_teams = registry_payload.get("fotmob_teams")
    if not isinstance(aliases, dict) or not isinstance(registry_teams, dict):
        print("[error] alias or registry payload has an unexpected shape", file=sys.stderr)
        return 1

    try:
        scanned_matches, schedule_names = collect_schedule_names()
    except Exception as error:  # noqa: BLE001 - schedule is the run's foundation
        print(f"[error] official schedule scan failed: {error}", file=sys.stderr)
        return 1

    pending_names = _read_pending_names()
    candidate_names = dict(pending_names)
    candidate_names.update(schedule_names)

    already_known = sum(1 for key in candidate_names if key in aliases)
    targets = [
        candidate_names[key] for key in candidate_names if key not in aliases
    ]

    print(
        f"[info] scanned_matches={scanned_matches} unique_names={len(candidate_names)} "
        f"already_known={already_known} to_resolve={len(targets)}",
        file=sys.stderr,
    )

    counter = {"llm_calls": 0}
    results = _resolve_names(targets, counter)
    now_iso = _local_now().isoformat()
    date_stamp = _local_now().strftime("%Y%m%d")

    planned_aliases = []
    planned_registry = []
    pending = []
    seen_keys = set()
    for name in targets:
        key = _team_key(name)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result, candidates, reason = results.get(name, (None, [], "no_candidate_passed"))
        if result is None:
            pending.append({"zh": name, "candidates": candidates, "reason": reason})
            continue
        entry = {
            "confirmation_method": "fotmob_search_verified_auto",
            "first_seen_at": now_iso,
            "verified_at": now_iso,
            "score": result["score"],
            "search_url": result["search_url"],
            "source_name": result["team_name"],
            "source_team_id": str(result["team_id"]),
            "source_url": result["source_url"],
        }
        registry_entry = {
            "score": result["score"],
            "search_url": result["search_url"],
            "source_name": result["team_name"],
            "source_team_id": str(result["team_id"]),
            "source_url": result["source_url"],
            "team_name": name,
        }
        planned_aliases.append((key, entry))
        if key not in registry_teams:
            planned_registry.append((key, registry_entry))

    if args.dry_run:
        for key, entry in planned_aliases:
            print(
                json.dumps(
                    {"planned": "source_aliases", "key": key, "entry": entry},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        for key, entry in planned_registry:
            print(
                json.dumps(
                    {"planned": "source_link_registry", "key": key, "entry": entry},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        for item in pending:
            print(
                json.dumps({"planned": "pending", "entry": item}, ensure_ascii=False),
                file=sys.stderr,
            )
        print("[info] dry-run: no bytes written", file=sys.stderr)
    else:
        for path in (ALIAS_PATH, REGISTRY_PATH):
            backup_path, status = _backup(path, date_stamp)
            print(f"[info] backup {backup_path.name}: {status}", file=sys.stderr)
        for key, entry in planned_aliases:
            aliases[key] = entry
        for key, entry in planned_registry:
            registry_teams[key] = entry
        if planned_aliases:
            _atomic_write_json(ALIAS_PATH, alias_payload, alias_trailing)
        if planned_registry:
            _atomic_write_json(REGISTRY_PATH, registry_payload, registry_trailing)
        pending_payload = {"generated_at": now_iso, "pending": pending}
        _atomic_write_json(
            PENDING_PATH,
            pending_payload,
            b"\n",
        )
        for key, _entry in planned_aliases:
            print(f"[info] added alias: {key}", file=sys.stderr)
        for item in pending:
            print(f"[info] pending: {item['zh']} ({item['reason']})", file=sys.stderr)

    summary = {
        "scanned_matches": scanned_matches,
        "unique_names": len(candidate_names),
        "already_known": already_known,
        "added": len(planned_aliases),
        "pending": len(pending),
        "llm_calls": counter["llm_calls"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
