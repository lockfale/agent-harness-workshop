"""P5: prefixes are stripped, local is free, and unknown models fail fast."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.pricing import PriceTable, UnknownModelError, Usage

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_gateway_prefix_is_stripped_before_lookup(prices):
    assert prices.strip_prefix("or/vendor/big") == "vendor/big"
    assert prices.strip_prefix("gw/vendor/big") == "vendor/big"
    assert prices.resolve("or/vendor/big").price.input == 10.0
    assert prices.resolve("vendor/big").price.input == 10.0


def test_a_vendor_segment_is_not_a_gateway_prefix(prices):
    """`vendor/` must survive, or every model collapses to its bare name."""
    assert prices.strip_prefix("vendor/big") == "vendor/big"
    assert prices.split_prefix("vendor/big") == (None, "vendor/big")


def test_a_bare_name_is_left_alone(prices):
    assert prices.strip_prefix("big") == "big"


def test_local_prefixes_cost_nothing(prices):
    for name in ("local/anything-at-all", "spark/glm-5.3-flash"):
        resolution = prices.resolve(name)
        assert resolution.local is True
        assert resolution.price.is_free
        assert prices.cost(name, Usage(input_tokens=10**7, output_tokens=10**7)) == 0.0


def test_an_unknown_model_raises_and_names_the_price_file(prices):
    with pytest.raises(UnknownModelError) as caught:
        prices.resolve("vendor/unlisted")

    message = str(caught.value)
    assert "vendor/unlisted" in message
    assert "test-prices.yaml" in message
    assert "will not guess" in message


def test_validate_reports_every_unpriced_model_at_once(prices):
    with pytest.raises(UnknownModelError) as caught:
        prices.validate(["vendor/big", "vendor/nope", "local/free", "vendor/also-nope"])

    message = str(caught.value)
    assert "vendor/nope" in message
    assert "vendor/also-nope" in message
    assert "vendor/big" not in message


def test_validate_accepts_a_fully_priced_run(prices):
    resolved = prices.validate(["or/vendor/big", "vendor/small", "local/mine"])
    assert [r.local for r in resolved] == [False, False, True]
    assert resolved[0].lookup_key == "vendor/big"
    assert resolved[0].prefix == "or"


def test_cost_bills_cached_tokens_at_the_cache_rate(prices):
    # 1M prompt tokens of which 600k were cached, 1M output.
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=600_000)
    # 400k fresh at $10, 600k cached at $1, 1M out at $30.
    assert prices.cost("vendor/big", usage) == pytest.approx(4.0 + 0.6 + 30.0)


def test_cost_without_a_cache_rate(prices):
    usage = Usage(input_tokens=2_000_000, output_tokens=500_000)
    assert prices.cost("vendor/small", usage) == pytest.approx(2.0 + 1.0)


def test_cached_tokens_never_exceed_the_prompt(prices):
    """A provider reporting more cached than prompt tokens must not go negative."""
    usage = Usage(input_tokens=1000, output_tokens=0, cache_read_tokens=5000)
    assert prices.cost("vendor/big", usage) >= 0.0


def test_the_shipped_price_table_loads_and_is_internally_consistent():
    table = PriceTable.load(REPO_ROOT / "config/prices.yaml")
    assert table.gateway_prefixes, "no gateway prefixes configured"
    assert table.local_prefixes, "no local prefixes configured"
    assert table.verified, "prices.yaml has no prices_verified date"
    # Every model the shipped roles.yaml names must be priced.
    import yaml

    roles = yaml.safe_load((REPO_ROOT / "config/roles.yaml").read_text(encoding="utf-8")) or {}
    names: list[str] = []
    for role in (roles.get("roles") or {}).values():
        names.append(role["model"])
        names.extend(role.get("panel") or [])
    table.validate(names)
