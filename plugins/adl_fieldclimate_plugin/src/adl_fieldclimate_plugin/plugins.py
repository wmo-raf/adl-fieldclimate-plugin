from adl.core.registries import Plugin


class FieldClimatePlugin(Plugin):
    type = "adl_fieldclimate_plugin"
    label = "ADL FieldClimate Plugin"
    
    def get_station_data(self, station_link, start_date=None, end_date=None):
        start_date_utc_format = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date_utc_format = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        field_climate_http_client = station_link.network_connection.get_api_client()
        
        records = field_climate_http_client.get_station_hourly_data(
            station_link.fc_station_code,
            start_date=start_date_utc_format,
            end_date=end_date_utc_format
        )
        
        return records
