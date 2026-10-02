from electrum.simple_config import SimpleConfig, ConfigVar

plugin_name = "browser"

# Page the Browser tab opens on. Empty means the built-in start page.
SimpleConfig.BROWSER_HOME_URL = ConfigVar(
    key='plugins.browser.home_url',
    default='',
    type_=str,
    plugin=plugin_name,
)
