from django.apps import AppConfig

from adl.core.registries import plugin_registry


class PluginNameConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "adl_fieldclimate_plugin"
    
    def ready(self):
        from .plugins import FieldClimatePlugin
        
        plugin_registry.register(FieldClimatePlugin())
