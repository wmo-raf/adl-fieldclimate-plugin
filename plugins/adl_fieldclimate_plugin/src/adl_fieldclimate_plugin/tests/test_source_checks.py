"""
Tests for the ingestion-diagnostic contracts: ``get_source_endpoint()``,
``check_source()``, ``check_station_source()``, the ``adl_sources_count``
duck-typed handover, and the exception stamping and retry behaviour in
``client.py``. See the "Ingestion Diagnostic Contracts" page in the ADL
developer guide.

All tests run without touching the database: model instances are built unsaved
and the HTTP layer is stubbed, so the seam under test is exactly the contract
core consumes. That is what ``SimpleTestCase`` buys here — Django still calls
``setup_databases()`` whatever the class, so the suite is run on this plugin's
own compose stack with ``make test`` from the repo root.
"""

import ast
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

import requests
from adl.core.source_checks import SourceCheckResult, SourceCheckStatus
from django.test import SimpleTestCase

from adl_fieldclimate_plugin.client import FieldClimateAPIClient, category_for_status
from adl_fieldclimate_plugin.client import STATIONS_PATH, Sensor, Station
from adl_fieldclimate_plugin.models import FieldClimateConnection, FieldClimateStationLink
from adl_fieldclimate_plugin.plugins import FieldClimatePlugin

API_HOST = "api.fieldclimate.com"

NOT_JSON = object()


class FakeResponse:
    """A stubbed ``requests`` response: status code, and a body that either
    parses or does not."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self.payload is NOT_JSON:
            # What an HTML login page reached through a redirect looks like
            # from here. requests' own JSONDecodeError is a ValueError too.
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeAPIClient:
    """A stubbed FieldClimate client that answers the one call a check makes."""

    def __init__(self, stations=None, sensors=None, error=None, hourly=None):
        self.stations = stations if stations is not None else []
        self.sensors = sensors if sensors is not None else []
        self.error = error
        self.hourly = hourly

    def get_user_stations(self):
        if self.error is not None:
            raise self.error
        return self.stations

    def get_station_sensors(self, station_id):
        if self.error is not None:
            raise self.error
        return self.sensors

    def get_station_hourly_data(self, station_id, start_date=None, end_date=None):
        if self.error is not None:
            raise self.error
        return self.hourly


def sensor(name="Air temperature", code=506):
    return Sensor(name=name, unit="°C", code=code, ch=1, is_active=True)


def make_connection(**kwargs):
    kwargs.setdefault("client_id", "client")
    kwargs.setdefault("client_secret", "secret")
    kwargs.setdefault("username", "user")
    kwargs.setdefault("password", "password")
    return FieldClimateConnection(**kwargs)


def make_station_link(connection=None, **kwargs):
    kwargs.setdefault("fc_station_code", "00203BDF")
    link = FieldClimateStationLink(**kwargs)
    link.network_connection = connection or make_connection()
    return link


def make_client(**kwargs):
    """A real client with a token already in hand.

    The constructor authenticates unconditionally, so it is stubbed out here —
    every test that cares about authentication drives it explicitly.
    """
    kwargs.setdefault("username", "user")
    kwargs.setdefault("password", "password")
    kwargs.setdefault("client_id", "client")
    kwargs.setdefault("client_secret", "secret")
    with mock.patch.object(FieldClimateAPIClient, "authenticate"):
        client = FieldClimateAPIClient(**kwargs)
    client.access_token = "token"
    client.token_expiry = datetime(2999, 1, 1, tzinfo=timezone.utc)
    return client


def stub_api_client(client):
    """Patch the client factory, capturing the arguments the check passed."""
    calls = []

    def factory(self, **kwargs):
        calls.append(kwargs)
        return client

    patcher = mock.patch.object(FieldClimateConnection, "get_api_client", autospec=True,
                                side_effect=factory)
    return patcher, calls


class GetApiClientTests(SimpleTestCase):
    """The factory's defaults are the ingestion path's behaviour, unchanged;
    only the on-demand checks ask for anything else."""

    def build(self, **kwargs):
        with mock.patch.object(FieldClimateAPIClient, "authenticate"):
            return make_connection().get_api_client(**kwargs)

    def test_defaults_are_todays_ingestion_behaviour(self):
        client = self.build()
        self.assertEqual(client.timeout, 30.0)
        self.assertEqual(client.max_attempts, 3)

    def test_checks_can_bound_the_call(self):
        # retries counts retries; the client counts attempts. Zero retries is
        # one attempt, which is what fits inside core's probe budget.
        client = self.build(timeout=5, retries=0)
        self.assertEqual(client.timeout, 5)
        self.assertEqual(client.max_attempts, 1)


class GetSourceEndpointTests(SimpleTestCase):

    def test_names_the_data_host_not_the_oauth_host(self):
        # Layer 4 is the path to the source, and the source is where the
        # observations come from.
        self.assertEqual(make_connection().get_source_endpoint(), (API_HOST, 443))

    def test_the_host_is_the_one_the_client_dials(self):
        endpoint_host, _port = make_connection().get_source_endpoint()
        self.assertIn(endpoint_host, FieldClimateAPIClient.API_BASE_URL)
        self.assertNotIn(endpoint_host, FieldClimateAPIClient.OAUTH_URL)


class CheckSourceTests(SimpleTestCase):

    def run_check(self, client, connection=None):
        connection = connection or make_connection()
        patcher, calls = stub_api_client(client)
        with patcher:
            result = connection.check_source()
        self.assertIsInstance(result, SourceCheckResult)
        self.assertIn(result.status, SourceCheckStatus.ALL)
        return result, calls

    def test_a_parsed_station_list_is_ok(self):
        stations = [Station(station_id="00203BDF", station_name="Kericho")]
        result, _calls = self.run_check(FakeAPIClient(stations=stations))
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIsNone(result.category)
        self.assertIn(API_HOST, result.message)
        self.assertIn("1", result.message)

    def test_bounds_the_call(self):
        _result, calls = self.run_check(FakeAPIClient(stations=[]))
        self.assertEqual(calls, [{"timeout": 5, "retries": 0}])

    def test_classifies_from_the_status_the_server_sent(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (500, "PROTOCOL_ERROR"),
                                 (503, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertEqual(result.category, category)
                self.assertIn(str(status), result.message)
                self.assertIn("/user/stations", result.message)

    def test_declines_a_status_that_is_not_the_sources_fault(self):
        for status in (400, 422, 429):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)

    def test_a_login_page_200_is_not_ok(self):
        # The body parsed but was not a station list, or did not parse at all —
        # either way the source said nothing we can trust. A token response
        # without a token lands here too, since the constructor authenticates.
        for error in (ValueError("The response carried no station list."),
                      ValueError("The token response carried no access token."),
                      requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)):
            with self.subTest(error=str(error)):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)
                self.assertIn("not a station list", result.message)

    def test_a_codeless_failure_declines_the_category(self):
        # Core stamps every return layer 5, so a layer-4 category here would
        # have the diagnostic contradict itself about which layer failed.
        for error in (requests.ConnectionError("connection refused"),
                      requests.exceptions.SSLError("bad handshake"),
                      requests.exceptions.ReadTimeout("timed out")):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)
                self.assertIn("could not be reached", result.message)

    def test_a_credential_fault_in_the_constructor_reads_as_a_check_failure(self):
        # The client authenticates in __init__, so construction is inside the
        # guarded region: this must be a verdict, not an unhandled error.
        connection = make_connection()
        error = requests.HTTPError(response=FakeResponse(401))
        with mock.patch.object(FieldClimateConnection, "get_api_client",
                               autospec=True, side_effect=error):
            result = connection.check_source()
        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertEqual(result.category, "AUTH_FAILED")

    def test_survives_the_core_normaliser(self):
        from adl.core.source_checks import normalise_source_check_result
        result, _calls = self.run_check(FakeAPIClient(stations=[]))
        self.assertEqual(normalise_source_check_result(result).status, SourceCheckStatus.OK)

    def test_core_detects_the_override(self):
        from adl.core.source_checks import connection_implements_check_source
        self.assertTrue(connection_implements_check_source(make_connection()))


class CheckStationSourceTests(SimpleTestCase):

    def run_check(self, client, link=None):
        link = link or make_station_link()
        patcher, calls = stub_api_client(client)
        with patcher:
            result = link.check_station_source()
        self.assertIsInstance(result, SourceCheckResult)
        self.assertIn(result.status, SourceCheckStatus.ALL)
        return result, calls

    def test_a_resolving_code_is_ok_with_the_sensor_count(self):
        result, _calls = self.run_check(FakeAPIClient(sensors=[sensor(), sensor(code=507)]))
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("00203BDF", result.message)
        self.assertIn("2", result.message)

    def test_zero_sensors_is_still_ok_with_the_zero_stated(self):
        # A station that exists but reports nothing is the operator's call, not
        # this check's.
        result, _calls = self.run_check(FakeAPIClient(sensors=[]))
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("0", result.message)

    def test_a_404_is_proven_not_found(self):
        error = requests.HTTPError(response=FakeResponse(404))
        result, _calls = self.run_check(FakeAPIClient(error=error))
        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertEqual(result.category, "PATH_NOT_FOUND")
        self.assertIn("00203BDF", result.message)

    def test_another_status_is_not_read_as_absence(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (500, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertEqual(result.category, category)

    def test_bounds_the_call(self):
        _result, calls = self.run_check(FakeAPIClient(sensors=[sensor()]))
        self.assertEqual(calls, [{"timeout": 5, "retries": 0}])

    def test_a_failed_read_is_never_converted_into_ok(self):
        for error in (requests.ConnectionError("connection refused"),
                      requests.exceptions.ReadTimeout("timed out"),
                      ValueError("The response carried no sensor list.")):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertNotEqual(result.category, "PATH_NOT_FOUND")

    def test_core_detects_the_override(self):
        from adl.core.source_checks import station_link_implements_check_station_source
        self.assertTrue(station_link_implements_check_station_source(make_station_link()))


class SourcesCountTests(SimpleTestCase):
    """The count is committed only from something the source told us, and only
    once it has told us."""

    START = datetime(2026, 8, 1, tzinfo=timezone.utc)
    END = datetime(2026, 8, 2, tzinfo=timezone.utc)

    def collect(self, link, client):
        patcher, _calls = stub_api_client(client)
        with patcher:
            return FieldClimatePlugin().get_station_data(link, self.START, self.END)

    def test_counts_the_rows_the_response_carried(self):
        link = make_station_link()
        rows = [{"observation_time": self.START}, {"observation_time": self.END}]
        records = self.collect(link, FakeAPIClient(hourly=(rows, 2)))
        self.assertEqual(link.adl_sources_count, 2)
        self.assertEqual(len(records), 2)

    def test_an_empty_answer_is_zero_not_silence(self):
        link = make_station_link()
        self.collect(link, FakeAPIClient(hourly=([], 0)))
        self.assertEqual(link.adl_sources_count, 0)

    def test_a_failed_call_makes_no_claim_at_all(self):
        # None, never 0: a run that never got an answer must not accuse the
        # source of having offered nothing.
        link = make_station_link()
        link.adl_sources_count = None
        with self.assertRaises(requests.ConnectionError):
            self.collect(link, FakeAPIClient(error=requests.ConnectionError("refused")))
        self.assertIsNone(link.adl_sources_count)

    def test_the_client_counts_the_parsed_rows(self):
        payload = {
            "dates": ["2026-08-01 00:00:00", "2026-08-01 01:00:00"],
            "data": [{"code": 506, "values": {"avg": [21.5, 22.0]}}],
        }
        client = make_client()
        with mock.patch.object(client, "_request", return_value=FakeResponse(200, payload)):
            rows, count = client.get_station_hourly_data("00203BDF", self.START, self.END)
        self.assertEqual(count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["506"], 21.5)


class RequestLoopTests(SimpleTestCase):
    """A wrapper must never be less classifiable than what it wraps."""

    @contextmanager
    def patched(self, client, responses=None, side_effect=None):
        """Stub the session and the backoff, handing back both mocks."""
        with mock.patch("adl_fieldclimate_plugin.client.time.sleep") as sleep:
            with mock.patch.object(client.session, "request",
                                   side_effect=side_effect or responses) as request:
                yield request, sleep

    def test_a_client_error_is_not_retried(self):
        # The old loop caught its own raise_for_status() in the generic
        # RequestException handler, so every 4xx was tried three times.
        client = make_client()
        with self.patched(client, responses=[FakeResponse(401)] * 3) as (request, sleep):
            with self.assertRaises(requests.HTTPError) as caught:
                client._request("GET", STATIONS_PATH)
        self.assertEqual(caught.exception.adl_category, "AUTH_FAILED")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_repeated_server_errors_keep_the_status(self):
        # The old loop raised a flat RuntimeError here, dropping the 503.
        client = make_client()
        with self.patched(client, responses=[FakeResponse(503)] * 3) as (request, sleep):
            with self.assertRaises(requests.HTTPError) as caught:
                client._request("GET", STATIONS_PATH)
        self.assertEqual(caught.exception.adl_category, "PROTOCOL_ERROR")
        self.assertEqual(caught.exception.adl_layer, 5)
        self.assertEqual(request.call_count, 3)
        # Backoff between attempts, never after the last one, which only ever
        # delayed the exception the caller was already getting.
        self.assertEqual(sleep.call_count, 2)

    def test_a_network_error_propagates_unwrapped(self):
        # Core reads ConnectTimeout as layer 4 and ReadTimeout as layer 5 off
        # the type alone; a wrapper here would delete that for free.
        client = make_client()
        error = requests.exceptions.ReadTimeout("timed out")
        with self.patched(client, side_effect=error):
            with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
                client._request("GET", STATIONS_PATH)
        self.assertFalse(hasattr(caught.exception, "adl_category"))

    def test_one_attempt_never_sleeps(self):
        client = make_client(max_attempts=1)
        with self.patched(client, responses=[FakeResponse(503)]) as (request, sleep):
            with self.assertRaises(requests.HTTPError):
                client._request("GET", STATIONS_PATH)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_a_successful_response_is_returned(self):
        client = make_client()
        with self.patched(client, responses=[FakeResponse(200, [])]):
            resp = client._request("GET", STATIONS_PATH)
        self.assertEqual(resp.status_code, 200)


class ExceptionStampingTests(SimpleTestCase):
    """A failed ingestion run carries the source's own verdict into the
    activity log, stamped in place so core's type table still applies."""

    def get_stations(self, response):
        client = make_client()
        with mock.patch.object(client.session, "request", return_value=response):
            return client.get_user_stations()

    def authenticate(self, response):
        client = make_client()
        with mock.patch.object(client.session, "post", return_value=response):
            return FieldClimateAPIClient.authenticate(client)

    def test_stamps_a_classified_status_at_layer_5(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (502, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                with mock.patch("adl_fieldclimate_plugin.client.time.sleep"):
                    with self.assertRaises(requests.HTTPError) as caught:
                        self.get_stations(FakeResponse(status))
                self.assertEqual(caught.exception.adl_category, category)
                self.assertEqual(caught.exception.adl_layer, 5)

    def test_stamps_the_token_exchange_too(self):
        # A code from the server is proof the server answered, so a 401 here is
        # AUTH_FAILED at layer 5 like any other. It used to be a flat Exception.
        with self.assertRaises(requests.HTTPError) as caught:
            self.authenticate(FakeResponse(401))
        self.assertEqual(caught.exception.adl_category, "AUTH_FAILED")
        self.assertEqual(caught.exception.adl_layer, 5)

    def test_leaves_a_declined_status_unstamped(self):
        # Declining keeps core's read-time tier free to classify the row later;
        # a stamp — UNKNOWN above all — would block it permanently.
        for status in (400, 422):
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError) as caught:
                    self.get_stations(FakeResponse(status))
                self.assertFalse(hasattr(caught.exception, "adl_category"))

    def test_core_reads_the_stamp(self):
        from adl.core.classification import classify_failure
        with self.assertRaises(requests.HTTPError) as caught:
            self.get_stations(FakeResponse(401))
        self.assertEqual(classify_failure(caught.exception), ("AUTH_FAILED", 5))

    def test_a_body_that_is_not_a_station_list_raises(self):
        for payload in (NOT_JSON, {"error": "unauthorized"}, None):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.get_stations(FakeResponse(200, payload))

    def test_a_token_response_without_a_token_raises(self):
        for payload in (NOT_JSON, {"error": "invalid_grant"}, {"access_token": ""}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.authenticate(FakeResponse(200, payload))

    def test_the_status_table_declines_what_is_not_the_sources_fault(self):
        self.assertIsNone(category_for_status(302))
        self.assertIsNone(category_for_status(429))
        self.assertEqual(category_for_status(404), "PATH_NOT_FOUND")


class OlderCoreImportSafetyTests(SimpleTestCase):
    """The plugin must import cleanly on a core release that predates the
    source-check contracts, so nothing may import ``adl.core.source_checks``
    at module level.

    The contracts import it lazily instead, inside the method that needs it.
    Never wrap that import in ``try/except ImportError``: on an older core the
    method is never called, so the handler is unreachable, and it would turn a
    genuine import failure into a silent "this plugin does not support the
    check".
    """

    # Every module this plugin ships. Extend it as the plugin grows more.
    MODULES = ["models.py", "plugins.py", "client.py", "apps.py", "views.py",
               "validators.py", "wagtail_hooks.py"]

    DENIED = "adl.core.source_checks"

    def test_no_module_level_import_of_source_checks(self):
        package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in self.MODULES:
            path = os.path.join(package_dir, name)
            if not os.path.exists(path):
                continue  # a module this plugin does not (yet) ship
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if node.col_offset != 0:
                    continue  # indented imports are lazy, inside a function
                names = [a.name for a in node.names]
                module = getattr(node, "module", "") or ""
                self.assertNotIn(
                    self.DENIED, [module] + names,
                    f"{name} imports {self.DENIED} at module level")
