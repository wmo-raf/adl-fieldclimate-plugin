from datetime import timezone

from adl.core.registries import Plugin


class FieldClimatePlugin(Plugin):
    type = "adl_fieldclimate_plugin"
    label = "ADL FieldClimate Plugin"

    def get_station_data(self, station_link, start_date=None, end_date=None):
        # Core hands the window over in the station's local timezone; the
        # client reads the bounds as UTC instants. Convert before formatting —
        # a bare strftime with a "Z" relabels local wall-clock time as UTC and
        # shifts the requested window by the station's offset, so on a UTC+3
        # connection the three hours after the latest saved record were never
        # asked for.
        start_date_utc_format = start_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date_utc_format = end_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        field_climate_http_client = station_link.network_connection.get_api_client()

        records, sources_count = field_climate_http_client.get_station_hourly_data(
            station_link.fc_station_code,
            start_date=start_date_utc_format,
            end_date=end_date_utc_format
        )

        # Duck-typed sources-count handover: core stores this on the run's
        # activity log so "looked, found nothing" (0) stays distinguishable from
        # "never looked" (None). Committed only here, once the response is
        # parsed — a call that raised leaves the attribute None, and core's
        # evidence rule abstains on NULL rather than blaming the source for a
        # run that never got an answer. The accumulate idiom is used even though
        # this plugin makes one call: there is no straight-assignment variant.
        if getattr(station_link, "adl_sources_count", None) is None:
            station_link.adl_sources_count = 0
        station_link.adl_sources_count += sources_count

        return records
