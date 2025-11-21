from adl.core.models import NetworkConnection, StationLink, DataParameter, Unit
from django.db import models
from django.utils.translation import gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.models import Orderable

from .client import FieldClimateAPIClient
from .validators import validate_start_date


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
    
    def get_api_client(self):
        """
        Returns the FieldClimate API client instance.
        """
        return FieldClimateAPIClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            username=self.username,
            password=self.password
        )


class FieldClimateStationLink(StationLink):
    """
    Model representing a link to a FieldClimate station.
    """
    
    fc_station_code = models.CharField(max_length=100, verbose_name=_("FieldClimate Station Code"))
    start_date = models.DateTimeField(blank=True, null=True, validators=[validate_start_date],
                                      verbose_name=_("Initial Collection start date"),
                                      help_text=_(
                                          "The date to start collection data for the first collection. "
                                          "Ignored if any data has been collected already for this station"), )
    
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


class FieldClimateStationLinkVariableMapping(Orderable):
    """
    Model representing a mapping between a FieldClimate station variable and an internal variable.
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
