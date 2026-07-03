from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

import pytest

from product_finder import sources
from product_finder.config import (
    AppConfig,
    ConfigError,
    ExtraSourceConfig,
    ItemConfig,
    SourcesConfig,
    load_config,
)
from product_finder.sources import Source
from product_finder.sources.rss import RssSource, extract_price, parse_feed


def make_item(**overrides):
    defaults = dict(name="Track Saw", terms=["track saw"], max_price=400)
    defaults.update(overrides)
    return ItemConfig(**defaults)


# --- Registry ------------------------------------------------------------------


def test_registry_default_builtins():
    registry = sources.build_registry(AppConfig())
    assert set(registry) == {"ebay", "gumtree", "facebook"}
    assert all(isinstance(s, Source) for s in registry.values())


def test_registry_respects_enabled_flags():
    cfg = AppConfig()
    cfg.sources.gumtree_enabled = False
    registry = sources.build_registry(cfg)
    assert "gumtree" not in registry


def test_registry_includes_extra_sources():
    cfg = AppConfig(
        sources=SourcesConfig(
            extra=[
                ExtraSourceConfig(name="johnpye", type="links",
                                  url="https://www.johnpye.co.uk/?s={term}"),
                ExtraSourceConfig(name="hukd", type="rss",
                                  url="https://example.com/rss?q={term}"),
                ExtraSourceConfig(name="off", type="links",
                                  url="https://example.com/?q={term}", enabled=False),
            ]
        )
    )
    registry = sources.build_registry(cfg)
    assert "johnpye" in registry and not registry["johnpye"].is_automated()
    assert "hukd" in registry and registry["hukd"].is_automated()
    assert "off" not in registry
    assert cfg.sources.enabled_names() == ["ebay", "gumtree", "facebook", "johnpye", "hukd"]


def test_every_source_honours_contract():
    cfg = AppConfig(
        sources=SourcesConfig(
            extra=[ExtraSourceConfig(name="x", type="links", url="https://x/?q={term}")]
        )
    )
    for source in sources.build_registry(cfg).values():
        assert isinstance(source.name, str) and source.name
        assert isinstance(source.is_automated(), bool)
        assert isinstance(source.manual_links(make_item()), list)


# --- URL-template (links) source ----------------------------------------------------


def test_url_template_placeholders():
    cfg = AppConfig(postcode="BL0 9AA", radius_miles=25)
    cfg.sources.extra = [
        ExtraSourceConfig(
            name="vinted", type="links", label="Vinted",
            url="https://v.example/catalog?q={term}&max={max_price}&pc={postcode}&r={radius}",
        )
    ]
    source = sources.build_registry(cfg)["vinted"]
    links = source.manual_links(make_item(terms=["track saw", "plunge saw"]))
    assert len(links) == 2
    assert links[0].label == "Vinted: track saw"
    assert links[0].url == "https://v.example/catalog?q=track+saw&max=400&pc=BL0+9AA&r=25"


# --- RSS source ---------------------------------------------------------------------


RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>feed</title>
<item><title>Makita SP6000 track saw £245</title><link>https://x/1</link>
  <guid>g1</guid><description>&lt;b&gt;Boxed&lt;/b&gt;, excellent condition</description></item>
<item><title>Free saw horse</title><link>https://x/2</link><guid>g2</guid>
  <description>no price mentioned</description></item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>feed</title>
<entry><id>a1</id><title>Festool TS55 £1,250.50</title>
  <link href="https://x/3"/><summary>Great deal</summary></entry>
</feed>"""


def test_extract_price():
    assert extract_price("Makita £245 saw") == 245.0
    assert extract_price("£1,250.50 bargain") == 1250.50
    assert extract_price("no price") is None


def test_parse_feed_rss_and_atom():
    rss = parse_feed(RSS_SAMPLE)
    assert len(rss) == 2
    assert rss[0]["title"].startswith("Makita")
    assert rss[0]["url"] == "https://x/1"
    assert "<b>" not in rss[0]["description"]  # HTML stripped
    atom = parse_feed(ATOM_SAMPLE)
    assert len(atom) == 1
    assert atom[0]["url"] == "https://x/3"


def test_rss_search_skips_priceless_entries():
    cfg = AppConfig()
    spec = ExtraSourceConfig(name="hukd", type="rss", url="https://h.example/rss?q={term}")
    source = RssSource(cfg, spec)
    response = mock.Mock(text=RSS_SAMPLE)
    response.raise_for_status = mock.Mock()
    with mock.patch("product_finder.sources.rss.requests.get", return_value=response) as get:
        listings = source.search("track saw", make_item())
    assert get.call_args.args[0] == "https://h.example/rss?q=track+saw"
    assert len(listings) == 1  # priceless entry skipped
    assert listings[0].source == "hukd"
    assert listings[0].price == 245.0
    assert listings[0].external_id == "g1"


# --- RSS entry age (published/updated dates) ------------------------------------


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt)


def test_parse_feed_extracts_dates():
    old = _rfc822(datetime.now(timezone.utc) - timedelta(days=800))
    rss = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
    <item><title>Old saw £100</title><link>https://x/1</link><guid>g1</guid>
      <pubDate>{old}</pubDate></item>
    <item><title>No date saw £100</title><link>https://x/2</link><guid>g2</guid></item>
    </channel></rss>"""
    entries = parse_feed(rss)
    assert entries[0]["published"].date() == (datetime.now(timezone.utc) - timedelta(days=800)).date()
    assert entries[1]["published"] is None

    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Deal £50</title>
    <link href="https://x/3"/><published>2024-01-15T10:00:00Z</published></entry></feed>"""
    entries = parse_feed(atom)
    assert entries[0]["published"] == datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)


def test_rss_search_drops_entries_older_than_max_age():
    stale = _rfc822(datetime.now(timezone.utc) - timedelta(days=800))
    fresh = _rfc822(datetime.now(timezone.utc) - timedelta(days=5))
    rss = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
    <item><title>Stale saw £100</title><link>https://x/1</link><guid>g1</guid>
      <pubDate>{stale}</pubDate></item>
    <item><title>Fresh saw £150</title><link>https://x/2</link><guid>g2</guid>
      <pubDate>{fresh}</pubDate></item>
    <item><title>Undated saw £200</title><link>https://x/3</link><guid>g3</guid></item>
    </channel></rss>"""
    cfg = AppConfig()
    spec = ExtraSourceConfig(
        name="hukd", type="rss", url="https://h.example/rss?q={term}", max_age_days=90
    )
    source = RssSource(cfg, spec)
    response = mock.Mock(text=rss)
    response.raise_for_status = mock.Mock()
    with mock.patch("product_finder.sources.rss.requests.get", return_value=response):
        listings = source.search("saw", make_item())
    # Stale entry dropped; fresh and undated (can't judge age) both kept.
    titles = {l.title for l in listings}
    assert titles == {"Fresh saw £150", "Undated saw £200"}


# --- Config validation -----------------------------------------------------------------


def _write_cfg(tmp_path, sources_yaml, item_sources=""):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"sources:\n{sources_yaml}\n"
        "projects:\n"
        "  - name: P\n"
        "    items:\n"
        "      - name: Widget\n"
        "        terms: [widget]\n"
        f"{item_sources}"
    )
    return path


def test_config_parses_extra_sources(tmp_path):
    cfg = load_config(_write_cfg(
        tmp_path,
        "  extra:\n"
        "    - name: johnpye\n"
        "      type: links\n"
        "      url: \"https://www.johnpye.co.uk/?s={term}\"\n",
        "        sources: [ebay, johnpye]\n",
    ))
    assert cfg.sources.extra[0].name == "johnpye"
    assert cfg.projects[0].items[0].sources == ["ebay", "johnpye"]


def test_config_rejects_bad_extra_type(tmp_path):
    with pytest.raises(ConfigError, match="unknown type"):
        load_config(_write_cfg(
            tmp_path,
            "  extra:\n    - name: x\n      type: scrape\n      url: \"https://x/{term}\"\n",
        ))


def test_config_rejects_url_without_term(tmp_path):
    with pytest.raises(ConfigError, match="must contain"):
        load_config(_write_cfg(
            tmp_path,
            "  extra:\n    - name: x\n      type: links\n      url: \"https://x/\"\n",
        ))


def test_config_parses_max_age_days(tmp_path):
    cfg = load_config(_write_cfg(
        tmp_path,
        "  extra:\n"
        "    - name: hukd\n"
        "      type: rss\n"
        "      url: \"https://h.example/rss?q={term}\"\n"
        "      max_age_days: 90\n",
    ))
    assert cfg.sources.extra[0].max_age_days == 90


def test_config_rejects_bad_max_age_days(tmp_path):
    with pytest.raises(ConfigError, match="max_age_days must be positive"):
        load_config(_write_cfg(
            tmp_path,
            "  extra:\n"
            "    - name: hukd\n"
            "      type: rss\n"
            "      url: \"https://h.example/rss?q={term}\"\n"
            "      max_age_days: 0\n",
        ))


def test_config_rejects_duplicate_source_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate source"):
        load_config(_write_cfg(
            tmp_path,
            "  extra:\n    - name: ebay\n      type: links\n      url: \"https://x/{term}\"\n",
        ))


def test_config_rejects_unknown_item_source(tmp_path):
    with pytest.raises(ConfigError, match="unknown sources"):
        load_config(_write_cfg(tmp_path, "  {}", "        sources: [nowhere]\n"))


# --- DB-backed source overrides (Sources page) ----------------------------------


def test_effective_sources_config_no_overrides_matches_yaml(tmp_path):
    from product_finder import db

    conn = db.connect(tmp_path / "t.db")
    cfg = AppConfig()  # defaults: ebay/gumtree/facebook all enabled
    eff = db.effective_sources_config(conn, cfg)
    assert eff.enabled_names() == cfg.sources.enabled_names()
    assert eff.ebay.app_id == ""


def test_set_source_enabled_overrides_builtin_and_extra(tmp_path):
    from product_finder import db

    conn = db.connect(tmp_path / "t.db")
    cfg = AppConfig(sources=SourcesConfig(
        extra=[ExtraSourceConfig(name="hukd", type="rss", url="https://h.example/rss?q={term}")]
    ))
    db.set_source_enabled(conn, "gumtree", False)
    db.set_source_enabled(conn, "hukd", False)
    eff = db.effective_sources_config(conn, cfg)
    assert "gumtree" not in eff.enabled_names()
    assert "hukd" not in eff.enabled_names()
    assert "ebay" in eff.enabled_names()  # untouched sources keep their YAML default

    # Re-enabling flips it back — overrides aren't one-way.
    db.set_source_enabled(conn, "gumtree", True)
    eff = db.effective_sources_config(conn, cfg)
    assert "gumtree" in eff.enabled_names()


def test_set_ebay_credentials_overlay_and_explicit_clear(tmp_path):
    from product_finder import db

    conn = db.connect(tmp_path / "t.db")
    cfg = AppConfig()
    db.set_ebay_credentials(conn, "app123", "cert456", "sandbox")
    eff = db.effective_sources_config(conn, cfg)
    assert eff.ebay.app_id == "app123"
    assert eff.ebay.cert_id == "cert456"
    assert eff.ebay.env == "sandbox"

    # Explicitly saving the form blank clears the override — falls back to
    # whatever YAML has (blank, in this default AppConfig).
    db.set_ebay_credentials(conn, "", "", "")
    eff = db.effective_sources_config(conn, cfg)
    assert eff.ebay.app_id == ""
    assert eff.ebay.cert_id == ""


def test_new_yaml_source_appears_without_any_db_action(tmp_path):
    """No import/seed step needed — a source added to YAML just shows up."""
    from product_finder import db

    conn = db.connect(tmp_path / "t.db")
    db.set_source_enabled(conn, "ebay", False)  # unrelated override already present
    cfg = AppConfig(sources=SourcesConfig(
        extra=[ExtraSourceConfig(name="newsite", type="links", url="https://n.example/?q={term}")]
    ))
    eff = db.effective_sources_config(conn, cfg)
    assert "newsite" in eff.enabled_names()
    assert "ebay" not in eff.enabled_names()


def test_run_once_honours_disabled_source_override(tmp_path):
    from product_finder import db, runner
    from product_finder.config import ProjectConfig

    conn = db.connect(tmp_path / "t.db")
    cfg = AppConfig(
        db_path=str(tmp_path / "t.db"),
        report_path=str(tmp_path / "reports" / "latest.md"),
        sources=SourcesConfig(
            extra=[ExtraSourceConfig(name="hukd", type="rss", url="https://h.example/rss?q={term}")]
        ),
        projects=[ProjectConfig(name="P", slug="p", items=[
            make_item(name="Widget", terms=["widget"], sources=["hukd"])
        ])],
    )
    db.set_source_enabled(conn, "hukd", False)
    with mock.patch("product_finder.sources.rss.requests.get") as get:
        runner.run_once(cfg, conn)
    get.assert_not_called()  # disabled via DB override, must never be fetched
