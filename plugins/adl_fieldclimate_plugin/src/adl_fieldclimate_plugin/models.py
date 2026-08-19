from urllib.parse import urlparse

import requests
from adl.core.models import NetworkConnection, StationLink, DataParameter, Unit
from django.db import models
from django.utils.translation import gettext, gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.models import Orderable

from .client import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    STATIONS_PATH,
    FieldClimateAPIClient,
    category_for_status,
)
from .validators import validate_start_date

# What the diagnostic's on-demand checks pass instead of the ingestion
# defaults. Core bounds its whole probe — DNS, TCP and the source check
# together — by a 15-second wall clock and abandons rather than kills a worker
# that overruns it, so the check has to come back first with a real verdict.
# This client's 30-second timeout with three exponentially backed-off attempts
# would blow that budget several times over. Deliberately not a model field: an
# operator who raised it to 300 for a slow partner would silently re-break the
# probe.
SOURCE_CHECK_TIMEOUT_SECONDS = 5
SOURCE_CHECK_RETRIES = 0


class FieldClimateConnection(NetworkConnection):
    """
    Model representing a connection to a FieldClimate API.
    """
    station_link_model_string_label = "adl_fieldclimate_plugin.FieldClimateStationLink"

    client_id = models.CharField(max_length=255, verbose_name="Client ID")
    client_secret = models.CharField(max_length=255, verbose_name="Client Secret")
    username = models.CharField(max_length=255, verbose_name="Username")
    password = models.CharField(max_length=255, verbose_name="Password")

    panels = NetworkConnection.panels + [
        MultiFieldPanel([
            FieldPanel("client_id"),
            FieldPanel("client_secret"),
            FieldPanel("username"),
            FieldPanel("password"),
        ], heading=_("FieldClimate API Credentials")),
    ]

    class Meta:
        verbose_name = _("FieldClimate API Connection")
        verbose_name_plural = _("FieldClimate API Connections")

    @property
    def source_host(self):
        """The data host this connection dials, for operator-facing messages."""
        return urlparse(FieldClimateAPIClient.API_BASE_URL).hostname

    def get_api_client(self, use_cache=True, timeout=DEFAULT_TIMEOUT,
                       retries=DEFAULT_RETRIES):
        """
        Returns the FieldClimate API client instance.

        The defaults are the ingestion path's behaviour, unchanged. The
        diagnostic's on-demand checks pass a bounded client instead. Nothing in
        this client caches, so `use_cache` is accepted for a uniform factory
        signature and has nothing to bypass — verified against every call it
        makes, and worth re-verifying if a cache is ever added.
        """
        return FieldClimateAPIClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            username=self.username,
            password=self.password,
            timeout=timeout,
            # The client counts attempts, the fleet's factory argument counts
            # retries: one try plus however many retries were asked for.
            max_attempts=retries + 1,
        )

    def get_source_endpoint(self):
        """
        The (host, port) core's generic DNS -> TCP probe dials (layer 4 of the
        ingestion diagnostic).

        The client hard-codes two hosts and this names the data one. A hard-coded
        host is not a guess — it is the literal string requests dials, so naming
        it is exactly as truthful as reading a model field would be, and no model
        field configures either host here. An outage of the OAuth host surfaces
        at layer 5 instead, with the host it failed to reach named in the
        message.
        """
        parsed = urlparse(FieldClimateAPIClient.API_BASE_URL)
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    def check_source(self):
        """
        Ask whether the source accepts our credentials and offers data (layer 5
        of the ingestion diagnostic). Read-only, on demand only.

        The client authenticates in its constructor, so building it is already
        half the check — which is exactly why construction belongs inside the
        guarded region, so a credential fault reads as a check failure rather
        than an unhandled error. The station list supplies the other half. A
        discrete token call would be cheaper and would prove only that half,
        letting a dead data API read OK two layers away from its cause.
        """
        # Imported lazily: this module does not exist on a core release
        # predating the source-check contracts, where this method is never
        # called and a module-level import would kill the whole plugin.
        from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

        host = self.source_host

        try:
            client = self.get_api_client(timeout=SOURCE_CHECK_TIMEOUT_SECONDS,
                                         retries=SOURCE_CHECK_RETRIES)
            stations = client.get_user_stations()
        except requests.HTTPError as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category=category_for_status(e.response.status_code),
                message=gettext("%(host)s returned HTTP %(code)s for %(path)s.") % {
                    "host": host,
                    "code": e.response.status_code,
                    "path": STATIONS_PATH,
                },
            )
        except ValueError:
            # Ordered ahead of RequestException on purpose: requests' own
            # JSONDecodeError is both, and it belongs here. The source sent no
            # code to classify from, so the category is declined.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s answered, but the response was not a station "
                                "list.") % {
                    "host": host,
                },
            )
        except requests.RequestException as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s could not be reached: %(error)s") % {
                    "host": host,
                    "error": e,
                },
            )

        return SourceCheckResult(
            status=SourceCheckStatus.OK,
            message=gettext("%(host)s accepted our credentials and returned "
                            "%(count)s station(s).") % {
                "host": host,
                "count": len(stations),
            },
        )


class FieldClimateStationLink(StationLink):
    """
    Model representing a link to a FieldClimate station.
    """

    fc_station_code = models.CharField(max_length=100, verbose_name=_("FieldClimate Station Code"))
    start_date = models.DateTimeField(
        blank=True,
        null=True,
        validators=[validate_start_date],
        verbose_name=_("Collection Start Date"),
        help_text=_(
            "Collection never starts before this date. On the first run it is "
            "the start of the backfill; afterwards, moving it forward past the "
            "latest saved record skips the gap. Leave empty to start from the "
            "last hour."
        ),
    )

    panels = StationLink.panels + [
        FieldPanel("fc_station_code"),
        FieldPanel("start_date"),
        InlinePanel("variable_mappings", label=_("Station Variable Mapping"), heading=_("Station Variable Mappings")),
    ]

    class Meta:
        verbose_name = _("FieldClimate Station Link")
        verbose_name_plural = _("FieldClimate Station Links")

    def get_variable_mappings(self):
        """
        Returns the variable mappings for this station link.
        """
        return self.variable_mappings.all()

    def get_first_collection_date(self):
        """
        Returns the first collection date for this station link.
        Returns None if no start date is set.
        """
        return self.start_date

    def check_station_source(self):
        """
        Ask whether this station's FieldClimate code resolves at the source
        (layer 5 of the ingestion diagnostic, station-scoped).

        The station-addressed sensor list is the cheapest read that proves
        existence without fetching observations. It was already implemented here
        and called from nowhere, so this is composition rather than a new vendor
        integration.
        """
        from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

        connection = self.network_connection
        host = connection.source_host

        try:
            client = connection.get_api_client(timeout=SOURCE_CHECK_TIMEOUT_SECONDS,
                                               retries=SOURCE_CHECK_RETRIES)
            sensors = client.get_station_sensors(self.fc_station_code)
        except requests.HTTPError as e:
            category = category_for_status(e.response.status_code)

            if e.response.status_code == 404:
                # A 404 on a station-addressed resource, against a base URL this
                # plugin has no reason to doubt, is proof rather than suspicion:
                # this station link can never ingest anything.
                message = gettext("Station %(code)s was not found at the source.") % {
                    "code": self.fc_station_code,
                }
            else:
                message = gettext("%(host)s returned HTTP %(code)s for station "
                                  "%(station)s.") % {
                    "host": host,
                    "code": e.response.status_code,
                    "station": self.fc_station_code,
                }

            return SourceCheckResult(status=SourceCheckStatus.FAILED,
                                     category=category, message=message)
        except ValueError:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s answered, but the response was not a sensor "
                                "list.") % {
                    "host": host,
                },
            )
        except requests.RequestException as e:
            # Never convert a failed read into OK — and never into a claim of
            # absence either, which we have no proof of here.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("Could not read the sensors for station %(code)s from "
                                "%(host)s: %(error)s") % {
                    "code": self.fc_station_code,
                    "host": host,
                    "error": e,
                },
            )

        # The sensor count is a byproduct of the call the check already made,
        # never a second call and never a count of our own variable mappings.
        # Zero is still OK, stated plainly, and left for the operator to judge:
        # a station that exists but reports nothing is their call, not this
        # check's.
        return SourceCheckResult(
            status=SourceCheckStatus.OK,
            message=gettext("Station %(code)s was found at the source, offering "
                            "%(count)s sensor(s).") % {
                "code": self.fc_station_code,
                "count": len(sensors),
            },
        )


class FieldClimateStationLinkVariableMapping(Orderable):
    """
    Maps a FieldClimate station variable onto an internal one.
    """
    station_link = ParentalKey(FieldClimateStationLink, on_delete=models.CASCADE,
                               related_name="variable_mappings", verbose_name=_("FieldClimate Station Link"))
    adl_parameter = models.ForeignKey(DataParameter, on_delete=models.CASCADE, verbose_name=_("ADL Parameter"))
    fc_variable_code = models.CharField(max_length=100, verbose_name=_("FieldClimate Variable Code"))
    fc_variable_unit = models.ForeignKey(Unit, on_delete=models.CASCADE, verbose_name=_("FieldClimate Parameter Unit"))

    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("fc_variable_code"),
        FieldPanel("fc_variable_unit"),
    ]

    class Meta:
        verbose_name = _("FieldClimate Station Link Variable Mapping")
        verbose_name_plural = _("FieldClimate Station Link Variable Mappings")

    @property
    def source_parameter_name(self):
        """
        Returns the code of the FieldClimate variable.
        """
        return self.fc_variable_code

    @property
    def source_parameter_unit(self):
        """
        Returns the unit of the FieldClimate variable.
        """
        return self.fc_variable_unit
