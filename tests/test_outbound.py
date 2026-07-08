"""Marketplace Outbound Gateway — pure unit tests (no Flask, no DB). See
outbound.py and docs/adr/0002-affiliate-link-redirect-and-tracking.md."""

from product_finder.config import AppConfig, OutboundConfig, SourcesConfig
from product_finder.outbound import (
    MarketplaceOutboundService,
    PassthroughAdapter,
    QueryParamAffiliateAdapter,
    is_safe_redirect_url,
)


# --- is_safe_redirect_url -----------------------------------------------------


def test_safe_url_accepted():
    assert is_safe_redirect_url("https://www.ebay.co.uk/itm/12345") is True


def test_http_scheme_accepted():
    assert is_safe_redirect_url("http://example.com/listing") is True


def test_empty_string_rejected():
    assert is_safe_redirect_url("") is False


def test_javascript_scheme_rejected():
    assert is_safe_redirect_url("javascript:alert(1)") is False


def test_data_scheme_rejected():
    assert is_safe_redirect_url("data:text/html,<script>alert(1)</script>") is False


def test_relative_path_rejected():
    assert is_safe_redirect_url("/not/absolute") is False


def test_scheme_only_no_netloc_rejected():
    assert is_safe_redirect_url("https://") is False


# --- PassthroughAdapter --------------------------------------------------------


def test_passthrough_adapter_returns_url_unchanged():
    adapter = PassthroughAdapter("gumtree")
    resolution = adapter.resolve("https://www.gumtree.com/p/tools/drill/12345")
    assert resolution.url == "https://www.gumtree.com/p/tools/drill/12345"
    assert resolution.affiliate_applied is False


# --- QueryParamAffiliateAdapter -------------------------------------------------


def test_query_param_adapter_injects_params():
    adapter = QueryParamAffiliateAdapter("ebay", {"campid": "12345", "customid": "product-finder"})
    resolution = adapter.resolve("https://www.ebay.co.uk/itm/98765")
    assert resolution.affiliate_applied is True
    assert resolution.url.startswith("https://www.ebay.co.uk/itm/98765?")
    assert "campid=12345" in resolution.url
    assert "customid=product-finder" in resolution.url


def test_query_param_adapter_preserves_existing_query_params():
    adapter = QueryParamAffiliateAdapter("ebay", {"campid": "12345"})
    resolution = adapter.resolve("https://www.ebay.co.uk/itm/98765?hash=abc")
    assert "hash=abc" in resolution.url
    assert "campid=12345" in resolution.url


def test_query_param_adapter_own_params_win_over_existing_same_name():
    adapter = QueryParamAffiliateAdapter("ebay", {"campid": "ours"})
    resolution = adapter.resolve("https://www.ebay.co.uk/itm/98765?campid=someone-elses")
    assert "campid=ours" in resolution.url
    assert "someone-elses" not in resolution.url


def test_query_param_adapter_with_no_params_configured_is_unchanged():
    adapter = QueryParamAffiliateAdapter("ebay", {})
    resolution = adapter.resolve("https://www.ebay.co.uk/itm/98765")
    assert resolution.url == "https://www.ebay.co.uk/itm/98765"
    assert resolution.affiliate_applied is False


def test_query_param_adapter_never_raises_on_malformed_url():
    adapter = QueryParamAffiliateAdapter("ebay", {"campid": "12345"})
    # urlsplit()/urlunsplit() are lenient enough that this doesn't actually
    # raise (garbage just becomes the "path") — the real assertion here is
    # simply that resolve() never blows up on a bad listings.url row, which
    # would take down the whole redirect endpoint.
    resolution = adapter.resolve("not a url \x00 at all")
    assert resolution.url  # didn't raise, produced *something*


# --- MarketplaceOutboundService -------------------------------------------------


def _cfg(affiliate_params=None) -> AppConfig:
    return AppConfig(
        sources=SourcesConfig(),  # ebay, gumtree, facebook enabled by default
        outbound=OutboundConfig(affiliate_params=affiliate_params or {}),
    )


def test_service_uses_passthrough_when_no_affiliate_config():
    service = MarketplaceOutboundService(_cfg())
    resolution = service.resolve("ebay", "https://www.ebay.co.uk/itm/1")
    assert resolution.url == "https://www.ebay.co.uk/itm/1"
    assert resolution.affiliate_applied is False


def test_service_injects_affiliate_params_when_configured():
    service = MarketplaceOutboundService(
        _cfg(affiliate_params={"ebay": {"campid": "12345"}})
    )
    resolution = service.resolve("ebay", "https://www.ebay.co.uk/itm/1")
    assert resolution.affiliate_applied is True
    assert "campid=12345" in resolution.url


def test_service_only_affects_configured_source_not_others():
    service = MarketplaceOutboundService(
        _cfg(affiliate_params={"ebay": {"campid": "12345"}})
    )
    resolution = service.resolve("gumtree", "https://www.gumtree.com/p/x/1")
    assert resolution.affiliate_applied is False
    assert resolution.url == "https://www.gumtree.com/p/x/1"


def test_service_unknown_source_fails_safe_to_original_url():
    # A source string with no registered adapter at all (stale data, or a
    # source since removed from config) must never block the redirect.
    service = MarketplaceOutboundService(_cfg())
    resolution = service.resolve("some-removed-source", "https://example.com/x")
    assert resolution.url == "https://example.com/x"
    assert resolution.affiliate_applied is False


def test_service_catches_adapter_exception_and_falls_back():
    service = MarketplaceOutboundService(_cfg())

    class BoomAdapter:
        name = "ebay"

        def resolve(self, listing_url):
            raise RuntimeError("boom")

    service._adapters["ebay"] = BoomAdapter()
    resolution = service.resolve("ebay", "https://www.ebay.co.uk/itm/1")
    assert resolution.url == "https://www.ebay.co.uk/itm/1"
    assert resolution.affiliate_applied is False


def test_service_registers_extra_config_sources_too():
    cfg = AppConfig(
        sources=SourcesConfig(),
        outbound=OutboundConfig(affiliate_params={"vinted": {"ref": "product-finder"}}),
    )
    from product_finder.config import ExtraSourceConfig

    cfg.sources.extra = [
        ExtraSourceConfig(name="vinted", type="links", url="https://vinted.example/{term}")
    ]
    service = MarketplaceOutboundService(cfg)
    resolution = service.resolve("vinted", "https://www.vinted.co.uk/catalog?search_text=drill")
    assert resolution.affiliate_applied is True
    assert "ref=product-finder" in resolution.url
