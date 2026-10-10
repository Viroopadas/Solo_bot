"""providers_config hook and USE_NEW_PAYMENT_FLOW extension."""

import pytest

from modules.inhouse.features.yookassa_methods import providers, state
from modules.inhouse.tests.helpers import activate, make_deps


STOCK = {
    "currency": "RUB",
    "value": "pay_yookassa",
    "fast": "process_custom_amount_input",
    "order": 10,
    "enabled": True,
}


def base_providers(enabled=True):
    return {
        "YOOKASSA": dict(STOCK, enabled=enabled),
        "YOOMONEY": {"currency": "RUB", "value": "pay_yoomoney", "fast": "x", "order": 20, "enabled": False},
    }


def merge(base, patch):
    """Same merge as handlers/payments/providers.get_providers_with_hooks."""
    out = {k: dict(v) for k, v in base.items()}
    for name, p in patch.items():
        if p is None:
            out.pop(name, None)
        else:
            cur = dict(out.get(name, {}))
            cur.update(p)
            out[name] = cur
    return out


@pytest.fixture
def active():
    activate(make_deps(None, None))


def test_fields_like_stock_yookassa(active):
    patch = providers.providers_patch(base_providers(), {"YOOKASSA": True})
    merged = merge(base_providers(), patch)
    for name in ("YOOKASSA_SBP", "YOOKASSA_CARD"):
        cfg = merged[name]
        assert cfg["module"] == "yookassa"
        assert cfg["fast"] == "process_custom_amount_input"
        assert cfg["value"] == "pay_yookassa"
        assert cfg["currency"] == "RUB"
        assert cfg["enabled"] is True
    assert merged["YOOKASSA_SBP"]["order"] == -30
    assert merged["YOOKASSA_CARD"]["order"] == -20
    assert merged["YOOKASSA"]["order"] == -10
    assert merged["YOOKASSA"]["value"] == "pay_yookassa"  # stock entry otherwise unchanged
    order = sorted(merged, key=lambda n: merged[n]["order"])
    assert order[:3] == ["YOOKASSA_SBP", "YOOKASSA_CARD", "YOOKASSA"]


def test_only_when_yookassa_enabled(active):
    assert providers.providers_patch(base_providers(enabled=False), {"YOOKASSA": False}) == {}
    assert providers.providers_patch({"YOOMONEY": {}}, {"YOOMONEY": True}) == {}


def test_flags_enable(active):
    assert providers.providers_patch(base_providers(enabled=False), {"YOOKASSA": True})


def test_inactive_or_module_off_returns_nothing(monkeypatch):
    assert providers.providers_patch(base_providers(), {"YOOKASSA": True}) == {}
    activate(make_deps(None, None))
    monkeypatch.setattr(state, "module_enabled", lambda: False)
    assert providers.providers_patch(base_providers(), {"YOOKASSA": True}) == {}


def test_new_flow_in_place_without_duplicates():
    flow = ["YOOKASSA"]
    same = flow
    added = providers.extend_new_flow(flow)
    assert flow is same and flow == ["YOOKASSA", "YOOKASSA_SBP", "YOOKASSA_CARD"]
    assert added == ["YOOKASSA_SBP", "YOOKASSA_CARD"]
    assert providers.extend_new_flow(flow) == [] and len(flow) == 3
    providers.revert_new_flow(flow, added)
    assert flow == ["YOOKASSA"] and flow is same


def test_new_flow_keeps_owner_entries():
    flow = ["YOOKASSA", "yookassa_sbp"]
    added = providers.extend_new_flow(flow)
    assert added == ["YOOKASSA_CARD"] and flow == ["YOOKASSA", "yookassa_sbp", "YOOKASSA_CARD"]
    providers.revert_new_flow(flow, added)
    assert flow == ["YOOKASSA", "yookassa_sbp"]


def test_new_flow_requires_list():
    with pytest.raises(TypeError):
        providers.extend_new_flow(("YOOKASSA",))
