"""Module-wide switches of inhouse.

A feature is turned on or off here; the change takes effect after a bot restart.
The installer uses the gitignored DISABLE_FILE for a local deployment rollback,
without editing tracked settings. The module switch in the admin panel also works: every part of every
feature checks it on each call.
"""

MODULE_NAME = "inhouse"

# Local deployment switch, ignored by git; no executable local config and no secrets.
from pathlib import Path

DISABLE_FILE = Path(__file__).resolve().parent / ".yookassa_methods.disabled"

FEATURES: dict[str, bool] = {
    # SBP / bank card / "SberPay and other" link buttons over the stock YooKassa provider.
    "yookassa_methods": True,
}
