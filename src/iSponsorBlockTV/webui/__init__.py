"""Optional web UI to edit ``config.json`` in the browser.

The web UI is off by default. It is enabled with environment variables
(see ``settings.py``) and runs next to the normal program, or on its own with the
``webui`` command.

Modules:

* ``settings``      - environment variables (port, host, password)
* ``config_schema`` - validation of submitted values, and the config as shown in the browser
* ``config_store``  - reading and writing ``config.json``
* ``security``      - password, CSRF check and security headers
* ``app``           - routes and request handlers
* ``server``        - starting and stopping the server
* ``static/``       - the page itself (HTML, CSS and JavaScript)
"""

from .server import run_standalone, start_from_env

__all__ = ["run_standalone", "start_from_env"]
