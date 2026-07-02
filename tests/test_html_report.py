from product_finder import db
from product_finder.alerts import html as html_report
from product_finder.config import AppConfig
from product_finder.models import Evaluation, Listing, ManualLink


def seed(conn, title="Makita SP6000 <track> saw", score=85.0, grade="A", flags=None, under_target=True):
    conn.execute("INSERT INTO projects (slug, name) VALUES ('p', 'Coachhouse & Tools')")
    conn.execute(
        "INSERT INTO items (project_id, name, priority, normal_price, target_deal_price) "
        "VALUES (1, 'Track Saw', 'high', 500, 300)"
    )
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="E1", title=title, price=245.0,
                url="https://example.com/1?a=b&c=d"),
    )
    db.record_match(
        conn, listing_id, 1,
        Evaluation(grade=grade, flags=flags or [], margin_abs=255.0, margin_pct=51.0,
                   under_target=under_target, deal_score=score),
    )
    conn.commit()


def test_html_report_content(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn)
    out = html_report.build_html(conn, AppConfig())
    assert "<!DOCTYPE html>" in out
    assert "Coachhouse &amp; Tools" in out           # project heading, escaped
    assert "Makita SP6000 &lt;track&gt; saw" in out  # title escaped
    assert "https://example.com/1?a=b&amp;c=d" in out
    assert 'class="excellent"' in out                # score 85, no flags
    assert "under target" in out
    assert "grade-A" in out


def test_html_report_warning_row(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, title="Faulty saw", score=25.0, grade="spares/repair",
         flags=["faulty", "spares or repairs"], under_target=True)
    out = html_report.build_html(conn, AppConfig())
    assert 'class="warning"' in out
    assert 'class="excellent"' not in out
    assert "grade-spares" in out
    assert "faulty" in out


def test_html_report_empty_and_manual_links(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    links = [ManualLink(source="gumtree", label="Gumtree: <saw>",
                        url="https://gumtree.com/search?q=saw&max=1")]
    out = html_report.build_html(conn, AppConfig(), links)
    assert "No matched listings yet" in out
    assert "Gumtree: &lt;saw&gt;" in out
    assert "q=saw&amp;max=1" in out


def test_write_html_report(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn)
    cfg = AppConfig(report_path=str(tmp_path / "reports" / "latest.md"))
    path = html_report.write_html_report(conn, cfg)
    assert path.name == "latest.html"
    assert path.parent == tmp_path / "reports"
    assert "<!DOCTYPE html>" in path.read_text()
