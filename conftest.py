"""Tests never read .env or the shell's app settings: they run on defaults only, so they always
use the fake Claude client and never load a real API key, whatever a developer's .env says.

This runs before any test module is imported, so every module that does
`from api.app.config import settings` gets the clean settings built here.
"""

import os
import tempfile

_repo = os.getcwd()
with tempfile.TemporaryDirectory() as _empty:
    os.chdir(_empty)  # config builds settings on import; there is no .env to read here
    try:
        from api.app import config
    finally:
        os.chdir(_repo)

for _name in config.Settings.model_fields:
    os.environ.pop(_name.upper(), None)

config.Settings.model_config["env_file"] = None  # also covers Settings(...) built inside tests
config.settings = config.Settings()
