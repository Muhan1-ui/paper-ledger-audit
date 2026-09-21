"""Shared HTTP transport settings for public evidence collectors."""

import gzip
import json
import os
from concurrent.futures import Future
from threading import Lock, Semaphore
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener


SOURCE_ACCESS_TARGETS = {
    "sporttery": "https://webapi.sporttery.cn/gateway/uniform/fb/getMatchDataPageListV1.qry?" + urlencode({"method": "concern", "clientCode": "3001"}),
    "433": "https://www.433.cool/odds/GetOddsSimple?pageindex=1&lastupdatetime=",
    "understat": "https://understat.com/getLeagueData/Serie%20A/2026",
    "sofascore": "https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-08-31",
    "thesportsdb": "https://www.thesportsdb.com/api/v1/json/123/searchevents.php?e=Arsenal_vs_Chelsea&d=2026-09-09",
}

SOURCE_ACCESS_HEADERS = {
    SOURCE_ACCESS_TARGETS["sporttery"]: {
        "Referer": "https://m.sporttery.cn/mjc/zqsj/?tab=concern",
    },
    SOURCE_ACCESS_TARGETS["understat"]: {
        "Referer": "https://understat.com/league/Serie_A",
        "X-Requested-With": "XMLHttpRequest",
    },
}


class SingleFlightCache:
    """Share one in-flight value between concurrent identical requests."""

    def __init__(self):
        self._lock = Lock()
        self._inflight = {}

    def get_or_load(self, key, loader):
        with self._lock:
            future = self._inflight.get(key)
            if future is None:
                future = Future()
                self._inflight[key] = future
                is_loader = True
            else:
                is_loader = False

        if not is_loader:
            return future.result()

        try:
            value = loader()
        except BaseException as error:
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(error)
            raise
        with self._lock:
            self._inflight.pop(key, None)
            future.set_result(value)
        return value


class PublicRequestCoordinator:
    """Deduplicate public URLs and cap in-flight requests for each provider domain."""

    def __init__(self, domain_limits):
        self._cache = SingleFlightCache()
        self._domain_limits = dict(domain_limits)
        self._lock = Lock()
        self._semaphores = {}

    def _semaphore_for(self, url):
        domain = urlparse(url).hostname or ""
        limit = self._domain_limits.get(domain, self._domain_limits.get("*", 4))
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("domain request limit must be a positive integer")
        with self._lock:
            return self._semaphores.setdefault(domain, Semaphore(limit))

    def get_or_load(self, url, loader):
        def limited_loader():
            semaphore = self._semaphore_for(url)
            with semaphore:
                return loader()

        return self._cache.get_or_load(url, limited_loader)


_PUBLIC_REQUESTS = PublicRequestCoordinator({
    "webapi.sporttery.cn": 4,
    "www.fotmob.com": 4,
    "understat.com": 2,
    "www.oddsportal.com": 1,
    "backend.oddsportal.com": 1,
    "www.433.cool": 2,
    "www.sofascore.com": 1,
    "www.thesportsdb.com": 2,
    "xgscore.io": 2,
    "api.open-meteo.com": 2,
    "*": 4,
})


def coordinated_public_request(url, loader):
    """Use the process-local public-source cache and provider concurrency limit."""
    return _PUBLIC_REQUESTS.get_or_load(url, loader)


def _windows_loopback_proxy():
    """Return the enabled Windows Internet Settings proxy only when it is local."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (FileNotFoundError, OSError, ValueError):
        return None
    if int(enabled) != 1 or not isinstance(server, str) or not server.strip():
        return None

    candidates = {}
    for entry in server.split(";"):
        key, separator, value = entry.partition("=")
        candidates[key.strip().casefold() if separator else "default"] = (
            value.strip() if separator else key.strip()
        )
    proxy = candidates.get("https") or candidates.get("http") or candidates.get("default")
    if not proxy:
        return None
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        return None
    try:
        if parsed.port is None:
            return None
    except ValueError:
        return None
    return parsed.geturl()


def _effective_proxy(environment):
    proxy = environment.get("TAICAI_HTTP_PROXY")
    if isinstance(proxy, str) and proxy.strip():
        return proxy.strip(), "project"
    proxy = _windows_loopback_proxy()
    if proxy:
        return proxy, "windows_loopback"
    return None, None


def proxy_mapping(environment):
    """Use an explicit project proxy or an enabled local Windows proxy."""
    proxy, _source = _effective_proxy(environment)
    if proxy is None:
        return {}
    return {"http": proxy, "https": proxy}


def _proxy_summary(environment):
    proxy, source = _effective_proxy(environment)
    if proxy is None:
        return {"configured": False}
    parsed = urlparse(proxy)
    summary = {"configured": True}
    if source == "windows_loopback":
        summary["source"] = source
    if parsed.scheme:
        summary["scheme"] = parsed.scheme
    if parsed.hostname:
        summary["host"] = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        summary["port"] = port
    return summary


def _probe_json_source(url, environment):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        **SOURCE_ACCESS_HEADERS.get(url, {}),
    }
    request = Request(url, headers=headers)
    opener = build_opener(ProxyHandler(proxy_mapping(environment)))
    with opener.open(request, timeout=12) as response:
        raw = response.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, (dict, list)):
        raise ValueError("public source did not return a JSON object or array")
    return payload


def source_access_report(environment=None, probe=None, targets=None):
    """Check public evidence source reachability without collecting or writing records."""
    environment = os.environ if environment is None else environment
    probe = _probe_json_source if probe is None else probe
    targets = SOURCE_ACCESS_TARGETS if targets is None else targets
    sources = {}
    for name, url in targets.items():
        result = {"url": url}
        try:
            probe(url, environment)
        except HTTPError as error:
            if error.code in {401, 403, 429}:
                result.update({"status": "access_restricted", "http_status": error.code})
            else:
                result.update({"status": "invalid_response", "http_status": error.code})
        except (URLError, OSError, TimeoutError):
            result["status"] = "network_error"
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            result["status"] = "invalid_response"
        else:
            result["status"] = "available"
        sources[name] = {"status": result.pop("status"), **result}
    return {"proxy": _proxy_summary(environment), "sources": sources}
