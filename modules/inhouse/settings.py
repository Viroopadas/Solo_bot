"""Module-wide switches of inhouse.

A feature is turned on or off here; the change takes effect after a bot restart
(systemctl restart bot.service). This is the main rollback: set the feature to False
and restart. The module switch in the admin panel also works: every part of every
feature checks it on each call.
"""

MODULE_NAME = "inhouse"

FEATURES: dict[str, bool] = {
    # SBP / bank card / "SberPay and other" link buttons over the stock YooKassa provider.
    "yookassa_methods": True,
}
