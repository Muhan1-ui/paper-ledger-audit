"""Public OddsPortal extraction helpers for paper-only odds evidence."""

import base64
import gzip
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import unquote, urljoin
from urllib.request import ProxyHandler, Request, build_opener

from .transport import coordinated_public_request

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


ODDSPORTAL_BASE_URL = "https://www.oddsportal.com"
BOOKMAKERS_URL = f"{ODDSPORTAL_BASE_URL}/ajax-my-bookmakers/"
PASSWORD = b"J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"

MARKET_SLUGS = {
    "1x2": 1,
    "over-under": 2,
    "ou": 2,
    "home-away": 3,
    "double-chance": 4,
    "ah": 5,
    "asian-handicap": 5,
    "dnb": 6,
    "to-qualify": 7,
    "correct-score": 8,
    "cs": 8,
    "ht-ft": 9,
    "half-time-full-time": 9,
    "odd-even": 10,
    "winner": 11,
    "european-handicap": 12,
    "bts": 13,
}

OPTION_LABELS = {
    1: {"0": "home_win", "1": "draw", "2": "away_win"},
    2: {"0": "over", "1": "under"},
    4: {"0": "home_or_draw", "1": "home_or_away", "2": "draw_or_away"},
    5: {"0": "home_handicap", "1": "away_handicap"},
    10: {"0": "odd", "1": "even"},
    12: {"0": "home_win", "1": "draw", "2": "away_win"},
    13: {"0": "yes", "1": "no"},
}

MARKET_NAMES = {
    1: "1X2",
    2: "Over/Under",
    3: "Home/Away",
    4: "Double Chance",
    5: "Asian Handicap",
    6: "Draw No Bet",
    7: "To Qualify",
    8: "Correct Score",
    9: "Half Time / Full Time",
    10: "Odd or Even",
    11: "Winner",
    12: "European Handicap",
    13: "Both Teams to Score",
}


def _http_get(url, referer=ODDSPORTAL_BASE_URL, accept="*/*"):
    def load():
        request = Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
            },
        )
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=20) as response:
            return response.read().decode("utf-8")

    return coordinated_public_request(url, load)


def decode_encrypted_response(encoded_text):
    """Decode the encrypted AJAX response used by OddsPortal public pages."""
    outer = base64.b64decode(encoded_text.strip()).decode("ascii")
    encrypted_payload, iv_hex = outer.split(":", 1)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=1000,
    ).derive(PASSWORD)
    decryptor = Cipher(
        algorithms.AES(key),
        modes.CBC(bytes.fromhex(iv_hex)),
    ).decryptor()
    padded = decryptor.update(base64.b64decode(encrypted_payload)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    payload = unpadder.update(padded) + unpadder.finalize()
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    return json.loads(payload.decode("utf-8"))


def parse_event_header(page_html):
    """Extract event identity and request metadata embedded in the event page."""
    match = re.search(r'id="react-event-header"\s+data=\'([^\']+)\'', page_html)
    if match is None:
        match = re.search(r'id="react-event-header"\s+data="([^"]+)"', page_html)
    if match is None:
        if "__NEXT_DATA__" in page_html or "/_next/" in page_html:
            raise ValueError(
                "OddsPortal current layout requires browser-visible named-bookmaker capture"
            )
        raise ValueError("OddsPortal event header was not found")
    payload = json.loads(html.unescape(match.group(1)))
    event_data = payload["eventData"]
    event_body = payload.get("eventBody", {})
    xhashf = unquote(html.unescape(event_data.get("xhashf", "")))
    return {
        "event_id": event_data["id"],
        "numeric_event_id": event_data.get("ukeyBase"),
        "sport_id": event_data.get("sportId", 1),
        "version_id": event_data.get("versionId", 1),
        "home_team": event_data.get("home"),
        "away_team": event_data.get("away"),
        "competition": event_data.get("tournamentName"),
        "country": event_data.get("countryName"),
        "kickoff_unix": event_body.get("startDate"),
        "default_bet_id": event_data.get("defaultBetId", 1),
        "default_scope_id": event_data.get("defaultScopeId", 2),
        "xhashf": xhashf,
        "request_pre_match_url": payload.get("requestPreMatch", {}).get("url"),
        "betting_types": event_body.get("additionalOddsInfo", {}).get("bettingTypes", {}),
    }


def _fragment_market(source_url, default_bet_id, default_scope_id):
    fragment = source_url.split("#", 1)[1] if "#" in source_url else ""
    if ":" not in fragment:
        return int(default_bet_id), int(default_scope_id)
    parts = fragment.split(":", 1)[1].split(";")
    slug = parts[0].strip().lower()
    betting_type_id = MARKET_SLUGS.get(slug, int(default_bet_id))
    scope_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else int(default_scope_id)
    return betting_type_id, scope_id


def build_match_event_url(
    event,
    source_url,
    timestamp_ms=None,
    betting_type_id=None,
    scope_id=None,
):
    """Build the encrypted pre-match odds AJAX URL for the requested fragment market."""
    if betting_type_id is None or scope_id is None:
        fragment_betting_type_id, fragment_scope_id = _fragment_market(
            source_url,
            event["default_bet_id"],
            event["default_scope_id"],
        )
        betting_type_id = fragment_betting_type_id if betting_type_id is None else betting_type_id
        scope_id = fragment_scope_id if scope_id is None else scope_id
    timestamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    path = (
        f"/match-event/{event['sport_id']}-{event['version_id']}-{event['event_id']}-"
        f"{betting_type_id}-{scope_id}-{event['xhashf']}.dat?_={timestamp}"
    )
    return urljoin(ODDSPORTAL_BASE_URL, path)


def extract_ajax_user_data_url(page_html):
    match = re.search(r'<script[^>]+src="([^"]*ajax-user-data[^"]+)"', page_html)
    if match is None:
        return None
    return urljoin(ODDSPORTAL_BASE_URL, html.unescape(match.group(1)))


def extract_bookmakers(decoded_payload):
    """Return provider-id to bookmaker-name mapping from decoded OddsPortal user data."""
    data = decoded_payload.get("d", {})
    rows = data.get("bookmakers", {}).get("data", [])
    bookmakers = {}
    for group in rows:
        if not isinstance(group, list):
            continue
        for row in group:
            provider_id = row.get("idProvider") if isinstance(row, dict) else None
            web_name = row.get("WebName") if isinstance(row, dict) else None
            if provider_id is not None and web_name:
                bookmakers[str(provider_id)] = web_name
    return bookmakers


def _decimal_string(value):
    return str(Decimal(str(value)))


def _average(values):
    decimals = [Decimal(str(value)) for value in values]
    if not decimals:
        return None
    return str(sum(decimals) / Decimal(len(decimals)))


def _line_label(row, market_name):
    parameter = row.get("mixedParameterName")
    if parameter:
        return str(parameter)
    handicap_value = row.get("handicapValue")
    if handicap_value not in (None, "", 0, "0", "0.00"):
        return f"{market_name} {handicap_value}"
    return market_name


def _option_labels(betting_type_id, row):
    if betting_type_id in (8, 9):
        return {"0": str(row.get("mixedParameterName") or "selection")}
    return OPTION_LABELS.get(int(betting_type_id), {"0": "option_0", "1": "option_1", "2": "option_2"})


def _provider_values(values):
    if isinstance(values, dict):
        return values.items()
    if isinstance(values, list):
        return ((str(index), value) for index, value in enumerate(values))
    if values is None:
        return ()
    return (("0", values),)


def summarize_market(decoded_payload, bookmakers=None):
    """Summarize decoded OddsPortal market rows into readable bookmaker prices."""
    bookmakers = bookmakers or {}
    data = decoded_payload.get("d", {})
    betting_type_id = int(data.get("bt") or 0)
    market_name = MARKET_NAMES.get(betting_type_id, f"Market {betting_type_id}")
    odds_data = data.get("oddsdata", {})
    back = odds_data.get("back", {}) if isinstance(odds_data, dict) else {}
    rows = []
    if not isinstance(back, dict):
        back = {}
    for row_id, row in back.items():
        option_labels = _option_labels(betting_type_id, row)
        bookmaker_rows = []
        prices_by_option = {label: [] for label in option_labels.values()}
        for provider_id, provider_prices in sorted(
            row.get("odds", {}).items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
        ):
            prices = {}
            for raw_option, raw_price in _provider_values(provider_prices):
                label = option_labels.get(str(raw_option), f"option_{raw_option}")
                prices[label] = _decimal_string(raw_price)
                prices_by_option.setdefault(label, []).append(raw_price)
            bookmaker_rows.append(
                {
                    "provider_id": str(provider_id),
                    "bookmaker": bookmakers.get(str(provider_id), f"provider:{provider_id}"),
                    "prices": prices,
                    "opening_prices": {
                        option_labels.get(str(option), f"option_{option}"): _decimal_string(price)
                        for option, price in _provider_values(row.get("openingOdd", {}).get(str(provider_id)))
                    },
                    "movement": {
                        option_labels.get(str(option), f"option_{option}"): direction
                        for option, direction in _provider_values(row.get("movement", {}).get(str(provider_id)))
                    },
                }
            )
        best_prices = {
            label: _decimal_string(max(values, key=lambda value: Decimal(str(value))))
            for label, values in prices_by_option.items()
            if values
        }
        worst_prices = {
            label: _decimal_string(min(values, key=lambda value: Decimal(str(value))))
            for label, values in prices_by_option.items()
            if values
        }
        rows.append(
            {
                "row_id": row_id,
                "label": _line_label(row, market_name),
                "handicap_value": row.get("handicapValue"),
                "bookmaker_count": len(bookmaker_rows),
                "average_prices": {
                    label: _average(values)
                    for label, values in prices_by_option.items()
                    if values
                },
                "best_prices": best_prices,
                "worst_prices": worst_prices,
                "bookmakers": bookmaker_rows,
            }
        )
    return {
        "betting_type_id": betting_type_id,
        "market_name": market_name,
        "scope_id": data.get("sc"),
        "time_base": data.get("time-base"),
        "rows": rows,
    }


def _source_url_for_market(source_url, market_slug, scope_id):
    if market_slug is None:
        return source_url
    if "#" not in source_url:
        return source_url
    base, fragment = source_url.split("#", 1)
    event_id = fragment.split(":", 1)[0].split(";", 1)[0]
    return f"{base}#{event_id}:{market_slug};{scope_id}"


def collect_oddsportal(source_url, market_slug=None, scope_id=None):
    """Fetch one public OddsPortal page and return decoded market evidence."""
    page_html = _http_get(source_url, accept="text/html")
    event = parse_event_header(page_html)
    betting_type_id = None
    if market_slug is not None:
        betting_type_id = MARKET_SLUGS.get(str(market_slug).lower())
        if betting_type_id is None:
            raise ValueError(f"unsupported OddsPortal market slug: {market_slug}")
        scope_id = 2 if scope_id is None else int(scope_id)
    bookmakers = {}
    bookmakers = extract_bookmakers(
        decode_encrypted_response(_http_get(BOOKMAKERS_URL, referer=source_url))
    )
    market_url = build_match_event_url(
        event,
        source_url,
        betting_type_id=betting_type_id,
        scope_id=scope_id,
    )
    raw_market_response = _http_get(market_url, referer=source_url)
    decoded_market = decode_encrypted_response(raw_market_response)
    summary = summarize_market(decoded_market, bookmakers)
    evidence_source_url = _source_url_for_market(source_url, market_slug, scope_id)
    return {
        "provider": "OddsPortal",
        "source_url": evidence_source_url,
        "page_source_url": source_url,
        "market_api_url": market_url,
        "market_raw_content_sha256": hashlib.sha256(
            raw_market_response.encode("utf-8")
        ).hexdigest(),
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "event": event,
        "bookmaker_source_url": BOOKMAKERS_URL,
        "bookmaker_count": len(bookmakers),
        "market": summary,
    }
