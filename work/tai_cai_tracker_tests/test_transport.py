import gzip
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

from outputs.tai_cai_tracker.transport import (
    PublicRequestCoordinator,
    SOURCE_ACCESS_TARGETS,
    SingleFlightCache,
    _probe_json_source,
    proxy_mapping,
    source_access_report,
)


class TransportTests(unittest.TestCase):
    def test_proxy_mapping_is_empty_without_project_proxy(self):
        with patch(
            "outputs.tai_cai_tracker.transport._windows_loopback_proxy",
            return_value=None,
        ):
            self.assertEqual(proxy_mapping({}), {})

    def test_proxy_mapping_uses_the_project_proxy_for_http_and_https(self):
        self.assertEqual(
            proxy_mapping({"TAICAI_HTTP_PROXY": "http://127.0.0.1:7891"}),
            {"http": "http://127.0.0.1:7891", "https": "http://127.0.0.1:7891"},
        )

    def test_proxy_mapping_uses_the_windows_loopback_proxy_when_project_proxy_is_unset(self):
        with patch(
            "outputs.tai_cai_tracker.transport._windows_loopback_proxy",
            return_value="http://127.0.0.1:7892",
        ):
            self.assertEqual(
                proxy_mapping({}),
                {"http": "http://127.0.0.1:7892", "https": "http://127.0.0.1:7892"},
            )

    def test_source_access_report_redacts_proxy_credentials_and_checks_all_sources(self):
        requested = []

        def probe(url, environment):
            requested.append((url, environment["TAICAI_HTTP_PROXY"]))
            return {"ok": True}

        report = source_access_report(
            {"TAICAI_HTTP_PROXY": "http://researcher:secret@127.0.0.1:7891"},
            probe=probe,
            targets={"official": "https://official.example.test/api", "xg": "https://xg.example.test/api"},
        )

        self.assertEqual(report["proxy"], {
            "configured": True,
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 7891,
        })
        self.assertEqual(requested, [
            ("https://official.example.test/api", "http://researcher:secret@127.0.0.1:7891"),
            ("https://xg.example.test/api", "http://researcher:secret@127.0.0.1:7891"),
        ])
        self.assertEqual(report["sources"], {
            "official": {"status": "available", "url": "https://official.example.test/api"},
            "xg": {"status": "available", "url": "https://xg.example.test/api"},
        })

    def test_source_access_report_classifies_access_network_and_invalid_response_failures(self):
        failures = iter((
            HTTPError("https://blocked.example.test", 429, "Too Many Requests", None, None),
            URLError("connection refused"),
            ValueError("not json"),
        ))

        def probe(url, environment):
            raise next(failures)

        with patch(
            "outputs.tai_cai_tracker.transport._windows_loopback_proxy",
            return_value=None,
        ):
            report = source_access_report(
                {},
                probe=probe,
                targets={
                    "blocked": "https://blocked.example.test",
                    "offline": "https://offline.example.test",
                    "malformed": "https://malformed.example.test",
                },
            )

        self.assertEqual(report["proxy"], {"configured": False})
        self.assertEqual(report["sources"]["blocked"], {
            "status": "access_restricted",
            "url": "https://blocked.example.test",
            "http_status": 429,
        })
        self.assertEqual(report["sources"]["offline"]["status"], "network_error")
        self.assertEqual(report["sources"]["malformed"]["status"], "invalid_response")

    def test_source_access_targets_match_the_collector_endpoints(self):
        self.assertEqual(
            SOURCE_ACCESS_TARGETS["understat"],
            "https://understat.com/getLeagueData/Serie%20A/2026",
        )
        self.assertEqual(
            SOURCE_ACCESS_TARGETS["thesportsdb"],
            "https://www.thesportsdb.com/api/v1/json/123/searchevents.php?e=Arsenal_vs_Chelsea&d=2026-09-09",
        )

    def test_sporttery_probe_uses_the_official_page_as_referer(self):
        response = MagicMock()
        response.read.return_value = b'{"errorCode": "0"}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("outputs.tai_cai_tracker.transport.build_opener", return_value=opener):
            _probe_json_source(SOURCE_ACCESS_TARGETS["sporttery"], {})

        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Referer"), "https://m.sporttery.cn/mjc/zqsj/?tab=concern")

    def test_source_probe_accepts_gzip_encoded_json(self):
        response = MagicMock()
        response.read.return_value = gzip.compress(b'{"teams": {}}')
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("outputs.tai_cai_tracker.transport.build_opener", return_value=opener):
            payload = _probe_json_source(SOURCE_ACCESS_TARGETS["understat"], {})

        self.assertEqual(payload, {"teams": {}})

    def test_single_flight_cache_runs_one_loader_for_concurrent_same_key_requests(self):
        cache = SingleFlightCache()
        started = threading.Event()
        calls = []

        def load():
            calls.append("load")
            started.set()
            time.sleep(0.05)
            return {"raw": "same response"}

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.get_or_load, "https://source.example.test/data", load)
            started.wait(timeout=1)
            second = executor.submit(cache.get_or_load, "https://source.example.test/data", load)
            values = [first.result(), second.result()]

        self.assertEqual(calls, ["load"])
        self.assertEqual(values, [{"raw": "same response"}, {"raw": "same response"}])

    def test_request_coordinator_caps_distinct_urls_from_one_domain(self):
        coordinator = PublicRequestCoordinator({"source.example.test": 1})
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def load():
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return "response"

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                coordinator.get_or_load, "https://source.example.test/one", load
            )
            second = executor.submit(
                coordinator.get_or_load, "https://source.example.test/two", load
            )
            self.assertEqual([first.result(), second.result()], ["response", "response"])

        self.assertEqual(maximum_active, 1)
