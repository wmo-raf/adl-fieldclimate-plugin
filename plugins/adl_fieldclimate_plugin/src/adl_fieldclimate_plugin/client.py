import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 3

# The same number stated as the fleet's factory argument means it: three
# attempts are one try plus two retries.
DEFAULT_RETRIES = DEFAULT_MAX_ATTEMPTS - 1

STATIONS_PATH = "/user/stations"

# The ingestion diagnostic's shared HTTP status table. The category strings are
# written out rather than imported from core: importing core's vocabulary would
# break this plugin at import time on an older core, and core drops any value it
# does not recognise anyway.
#
# 400 and 422 decline because a malformed request is our bug, 429 because rate
# limiting is our polling schedule, and 3xx because a redirect says nothing
# about the source. Nothing here ever stamps UNKNOWN: declining leaves core's
# read-time classification free to do better later, and a stamp does not.
STATUS_CATEGORIES = {
    401: "AUTH_FAILED",
    403: "PERMISSION_DENIED",
    404: "PATH_NOT_FOUND",
}


def category_for_status(status_code):
    """The diagnostic failure category for an HTTP status, or None where the
    status carries no honest one."""
    if status_code in STATUS_CATEGORIES:
        return STATUS_CATEGORIES[status_code]
    if status_code is not None and 500 <= status_code < 600:
        return "PROTOCOL_ERROR"
    return None


def _raise_for_status(response):
    """``raise_for_status()``, tagging the raised error for the diagnostic.

    The exception is stamped in place rather than wrapped, so the original type
    still matches core's own exception table and the traceback survives. A code
    from the server is proof the server answered, which is what makes every
    category derived from one layer 5.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        category = category_for_status(e.response.status_code)
        if category:
            e.adl_category = category
            e.adl_layer = 5
        raise


@dataclass
class Station:
    station_id: str
    station_name: Optional[str]


@dataclass
class Sensor:
    name: str
    unit: Optional[str]
    code: Optional[int]
    ch: Optional[int]
    is_active: Optional[bool]


class FieldClimateAPIClient:
    OAUTH_URL = "https://oauth.fieldclimate.com/token"
    API_BASE_URL = "https://api.fieldclimate.com/v2"

    def __init__(
            self,
            username: str,
            password: str,
            client_id: str,
            client_secret: str,
            scope: str = "basic",
            max_attempts: int = DEFAULT_MAX_ATTEMPTS,
            backoff_base: float = 1.0,
            timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        FieldClimate API client.

        :param username: OAuth username.
        :param password: OAuth password.
        :param client_id: OAuth client_id.
        :param client_secret: OAuth client_secret.
        :param scope: OAuth scope, default "basic".
        :param max_attempts: Total attempts for transient errors (5xx, network).
            One means try once and do not retry, which is what the diagnostic's
            on-demand checks ask for.
        :param backoff_base: Base seconds for exponential backoff.
        :param timeout: Request timeout in seconds.
        """
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None

        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.timeout = timeout

        # Reuse TCP connections
        self.session = requests.Session()

        # Authenticate immediately
        self.authenticate()

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------
    def authenticate(self) -> None:
        """Authenticate using the OAuth password grant and store tokens."""
        payload = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
            "scope": self.scope,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        logger.debug("Authenticating with password grant to FieldClimate")
        resp = self.session.post(self.OAUTH_URL, data=payload, timeout=self.timeout)

        if not resp.ok:
            # The body is not logged: it is the one place a credential could be
            # echoed back at us, and core's redaction never sees our own logger.
            logger.error("Authentication failed with HTTP %s", resp.status_code)

        # Raised in place, so a code the server sent still classifies. A flat
        # Exception here would have been unclassifiable by construction.
        _raise_for_status(resp)

        data = resp.json()
        self._store_token_data(data)

    def refresh(self) -> None:
        """
        Refresh the access token using refresh_token if available.
        Fallback to password authentication if refresh fails or no refresh_token.
        """
        if not self.refresh_token:
            logger.debug("No refresh_token available, re-authenticating with password.")
            self.authenticate()
            return

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        logger.debug("Refreshing access token with refresh_token")
        resp = self.session.post(self.OAUTH_URL, data=payload, timeout=self.timeout)

        if resp.status_code != 200:
            logger.warning("Refresh token failed (%s), falling back to password auth", resp.status_code)
            self.authenticate()
            return

        data = resp.json()
        self._store_token_data(data)

    def _store_token_data(self, data: Dict[str, Any]) -> None:
        # A 2xx is not proof of a token: requests follows redirects, so an
        # expired session landing on an HTML login page arrives here as a clean
        # 200, and a JSON body without the key is the same non-answer.
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ValueError("The token response carried no access token.")

        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        self.token_expiry = datetime.now(UTC) + timedelta(seconds=data["expires_in"])
        logger.debug("Token stored: expires at %s", self.token_expiry)

    def _ensure_token(self) -> None:
        """Refresh or re-authenticate if token expired."""
        if not self.token_expiry or datetime.now(UTC) >= self.token_expiry:
            logger.debug("Access token expired or missing, refreshing.")
            self.refresh()

    def _headers(self) -> Dict[str, str]:
        """Return authorization headers."""
        self._ensure_token()
        if not self.access_token:
            raise RuntimeError("Access token not available after authentication.")
        return {"Authorization": f"Bearer {self.access_token}"}

    # -------------------------------------------------------------------------
    # Low-level request wrapper with retries
    # -------------------------------------------------------------------------
    def _request(
            self,
            method: str,
            path: str,
            *,
            params: Optional[Dict[str, Any]] = None,
            data: Optional[Dict[str, Any]] = None,
            json_body: Optional[Any] = None,
    ) -> requests.Response:
        """
        Internal request helper with retries and exponential backoff.
        `path` is appended to API_BASE_URL.
        """
        url = f"{self.API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

        # The last thing that went wrong: either an exception requests raised,
        # or a transient response we are about to retry. Whichever it is, it is
        # what gets re-raised once the attempts run out — never a flat wrapper
        # that would throw its classification away.
        last_failure: Optional[Any] = None

        for attempt in range(self.max_attempts):
            try:
                self._ensure_token()
                logger.debug("HTTP %s %s (attempt %s)", method, url, attempt + 1)

                resp = self.session.request(
                    method.upper(),
                    url,
                    headers=self._headers(),
                    params=params,
                    data=data,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.HTTPError:
                # A code from the server is the source's answer, not a transient
                # network fault: retrying it costs time and changes nothing.
                # This is how an authentication failure gets out of _ensure_token
                # in one attempt rather than three.
                raise
            except requests.RequestException as exc:
                # Propagated unwrapped when the attempts run out. Core reads
                # ConnectTimeout as layer 4 and ReadTimeout as layer 5 off the
                # type alone, and a wrapper here would delete that.
                last_failure = exc
                logger.warning(
                    "Network error for %s %s on attempt %s: %s",
                    method,
                    url,
                    attempt + 1,
                    exc,
                )
            else:
                # No retry on client errors (4xx) except 429
                if resp.status_code < 500 and resp.status_code != 429:
                    if not resp.ok:
                        logger.error(
                            "Request failed: %s %s -> %s",
                            method,
                            url,
                            resp.status_code,
                        )
                    _raise_for_status(resp)
                    return resp

                # Retry on 5xx or 429
                last_failure = resp
                logger.warning(
                    "Transient error (status %s) for %s %s, attempt %s",
                    resp.status_code,
                    method,
                    url,
                    attempt + 1,
                )

            # Backoff before the next attempt — never after the last one, which
            # only ever delayed the exception the caller was already getting.
            if attempt + 1 < self.max_attempts:
                time.sleep(self.backoff_base * (2 ** attempt))

        if isinstance(last_failure, Exception):
            raise last_failure

        if last_failure is not None:
            # Repeated 5xx or 429. Raising the response's own error keeps the
            # status, so a 503 still reports as the source's fault; the old flat
            # RuntimeError dropped it and left the run unclassifiable.
            _raise_for_status(last_failure)

        raise RuntimeError(f"Failed to call {method} {url} after {self.max_attempts} attempts")

    # -------------------------------------------------------------------------
    # Public API methods
    # -------------------------------------------------------------------------
    def get_user_stations(self) -> List[Station]:
        """
        Fetch the user's stations from /user/stations and return basic summaries.
        """
        resp = self._request("GET", STATIONS_PATH)
        stations_resp = resp.json()

        # A 2xx is not proof of an API response, so the shape is checked here
        # once, for both the ingestion path and the source check.
        if not isinstance(stations_resp, list):
            raise ValueError("The response carried no station list.")

        summaries: List[Station] = []
        for st in stations_resp:
            name_info = st.get("name", {})
            summaries.append(
                Station(
                    station_id=name_info.get("original"),
                    station_name=name_info.get("custom"),
                )
            )

        return summaries

    def get_station_sensors(self, station_id: str) -> List[Sensor]:
        """
        Fetch sensors for a station from /station/{station_id}/sensors
        and return simplified Sensor objects.
        """
        resp = self._request("GET", f"/station/{station_id}/sensors")
        sensors = resp.json()

        if not isinstance(sensors, list):
            raise ValueError("The response carried no sensor list.")

        return [
            Sensor(
                name=s.get("name"),
                unit=s.get("unit"),
                code=s.get("code"),
                ch=s.get("ch"),
                is_active=s.get("isActive"),
            )
            for s in sensors
        ]

    def get_station_hourly_data(
            self,
            station_id: str,
            start_date: Any,
            end_date: Any,
    ) -> tuple:
        """
        Fetch hourly data for a station between two dates and return formatted rows.

        Returns ``(rows, sources_count)``. The count is of the rows the response
        carried for the requested window — the API restricts to the window
        itself — taken once the body is parsed and before any mapping or unit
        conversion, so nothing we do downstream can make the source look empty.
        It leaves the client by return value because the station link it is
        reported on belongs to the plugin, not here.

        start_date and end_date can be:
        - datetime objects
        - strings like "2025-01-01 00:00:00"
        - strings like "2025-01-01"
        """

        def to_dt(x: Any) -> datetime:
            if isinstance(x, datetime):
                dt = x
            else:
                # Accept both full datetime and date-only ISO formats
                dt = datetime.fromisoformat(str(x))

            # Assume UTC if no timezone is provided
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt

        start_dt = to_dt(start_date)
        end_dt = to_dt(end_date)

        if end_dt <= start_dt:
            raise ValueError("end_date must be after start_date")

        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        path = f"/data/{station_id}/hourly/from/{start_ts}/to/{end_ts}"
        resp = self._request("GET", path)
        raw_data = resp.json()

        rows = self._format_hourly_data(raw_data)

        return rows, len(rows)

    # -------------------------------------------------------------------------
    # Formatting helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _format_hourly_data(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Convert FieldClimate hourly data API response to list of dicts.
        Each dict represents a timestamped row of sensor data.
        """
        if not isinstance(raw, dict):
            raise ValueError("The response carried no hourly data.")

        date_strings: List[str] = raw.get("dates", [])
        sensors: List[Dict[str, Any]] = raw.get("data", [])

        # Convert timestamps to timezone-aware datetime objects (assume UTC)
        dates: List[datetime] = [
            datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            for ts in date_strings
        ]

        # Prepare empty rows
        rows: List[Dict[str, Any]] = [{"observation_time": dt} for dt in dates]

        for sensor in sensors:
            code = sensor.get("code")
            if code is None:
                # Some "calculation" or model outputs might not have codes
                continue

            code_str = str(code)

            values_dict = sensor.get("values", {})
            if not values_dict:
                continue

            # Typically one aggregation key: "avg", "sum", "last", "time", etc.
            aggr_type = next(iter(values_dict.keys()))
            sensor_values: Iterable[Any] = values_dict[aggr_type]

            for i, v in enumerate(sensor_values):
                # Ensure we don't index beyond available timestamps
                if i < len(rows):
                    rows[i][code_str] = v

        return rows
