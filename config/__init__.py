"""Configuration loading.

Two kinds of settings, deliberately kept apart:

* ``config.yaml``  — thresholds and rules. Committed to git, so every change to
  the system's behaviour shows up in the repo history.
* ``.env``         — API keys and passwords. Never committed (see .gitignore).
"""

from .loader import Config, load_config, secret, require_secret

__all__ = ["Config", "load_config", "secret", "require_secret"]
