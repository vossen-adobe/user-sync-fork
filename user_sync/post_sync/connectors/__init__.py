from .sign_sync import SignConnector

__CONNECTORS__ = {
    'sign_sync': SignConnector
}


def get_connector(connector_name, connector_config):
    connector = __CONNECTORS__[connector_name]
    return connector(connector_config)
