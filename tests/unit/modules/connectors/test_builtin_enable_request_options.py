"""Guard: every provider option that ``ConnectorConfig`` accepts is accepted by the builtin enable request."""

from modules.connectors.builtins import BuiltinEnableRequest
from modules.connectors.provider_options import ALL_OPTION_NAMES
from modules.connectors.service.schemas import ConnectorConfig


def test_builtin_enable_request_mirrors_every_connector_option() -> None:
    """``_desired_configuration`` reads each option off the request, so none may be missing."""
    missing = sorted(
        name
        for name in ALL_OPTION_NAMES
        if name in ConnectorConfig.model_fields and name not in BuiltinEnableRequest.model_fields
    )
    assert missing == []
