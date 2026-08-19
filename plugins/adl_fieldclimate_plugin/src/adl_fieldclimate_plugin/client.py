import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)


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
            max_retries: int = 3,
            backoff_base: float = 1.0,
            timeout: float = 30.0,
    ) -> None:
        """
        FieldClimate API client.

        :param username: OAuth username.
        :param password: OAuth password.
        :param client_id: OAuth client_id.
        :param client_secret: OAuth client_secret.
        :param scope: OAuth scope, default "basic".
        :param max_retries: Retries for transient errors (5xx, network).
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

        self.max_retries = max_retries
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
        if resp.status_code != 200:
            logger.error("Authentication failed: %s", resp.text)
            raise Exception(f"Authentication failed: {resp.text}")

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
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries):
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

                # No retry on client errors (4xx) except 429
                if resp.status_code < 500 and resp.status_code != 429:
                    if not resp.ok:
                        logger.error(
                            "Request failed: %s %s -> %s %s",
                            method,
                            url,
                            resp.status_code,
                            resp.text,
                        )
                        resp.raise_for_status()
                    return resp

                # Retry on 5xx or 429
                logger.warning(
                    "Transient error (status %s) for %s %s, attempt %s",
                    resp.status_code,
                    method,
                    url,
                    attempt + 1,
                )

            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Network error for %s %s on attempt %s: %s",
                    method,
                    url,
                    attempt + 1,
                    exc,
                )

            # Backoff before retry
            sleep_seconds = self.backoff_base * (2 ** attempt)
            time.sleep(sleep_seconds)

        if last_exc:
            raise last_exc

        # If we reach here, we had repeated server errors
        raise RuntimeError(f"Failed to call {method} {url} after {self.max_retries} attempts")

    # -------------------------------------------------------------------------
    # Public API methods
    # -------------------------------------------------------------------------
    def get_user_stations(self) -> List[Station]:
        """
        Fetch the user's stations from /user/stations and return basic summaries.
        """
        resp = self._request("GET", "/user/stations")
        stations_resp = resp.json()

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
    ) -> List[Dict[str, Any]]:
        """
        Fetch hourly data for a station between two dates and return formatted rows.

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

        return self._format_hourly_data(raw_data)

    # -------------------------------------------------------------------------
    # Formatting helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _format_hourly_data(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Convert FieldClimate hourly data API response to list of dicts.
        Each dict represents a timestamped row of sensor data.
        """
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
