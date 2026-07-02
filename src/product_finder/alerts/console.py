"""Console alerts for new matches."""

from __future__ import annotations

from ..models import MatchAlert


def format_alert(alert: MatchAlert) -> str:
    ev = alert.evaluation
    listing = alert.listing
    parts = [
        f"[{ev.deal_score:.0f}] {alert.project_name} / {alert.item_name}",
        f"  {listing.title}",
        f"  £{listing.price:,.2f}"
        + (f" (normal £{alert.normal_price:,.0f}, save {ev.margin_pct:.0f}%)" if alert.normal_price else ""),
        f"  Grade: {ev.grade}"
        + (" | UNDER TARGET" if ev.under_target else "")
        + (f" | ⚠ {', '.join(ev.flags)}" if ev.flags else ""),
        f"  {listing.source} — {listing.url}",
    ]
    return "\n".join(parts)


def send(alert: MatchAlert) -> None:
    print("\n" + format_alert(alert))
