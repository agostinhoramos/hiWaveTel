"""OpenAPI (drf-spectacular) registration for custom API key authentication."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """Maps ApiKeyAuthentication to an OpenAPI apiKey security scheme (header X-API-Key)."""

    target_class = 'apps.external_device.authentication.ApiKeyAuthentication'
    name = 'apiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': 'X-API-Key',
            'description': (
                'Device API key from POST /api/v1/external-devices/register/ (shown once). '
                'Alternatively: Authorization: ApiKey <key>'
            ),
        }
