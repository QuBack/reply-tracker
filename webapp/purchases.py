"""User-facing procurement summaries, scoped to an already authenticated database."""
from __future__ import annotations

from datetime import date

FILTERS = [("all", "Все"), ("waiting", "Ждём ответы"), ("compare", "Можно сравнить"), ("draft", "Черновики")]


def purchase_rows(database):
    rows = [dict(row) for row in database.list_campaigns()]
    with database.connect() as connection:
        stats = {row["campaign_id"]: row for row in connection.execute("""
            SELECT m.campaign_id, COUNT(DISTINCT m.recipient_id) AS response_count,
                   COUNT(DISTINCT CASE WHEN a.is_allowed=1 AND a.is_selected=1
                     AND a.duplicate_of_id IS NULL THEN m.recipient_id END) AS offer_count
            FROM incoming_messages m LEFT JOIN attachments a ON a.incoming_message_id=m.id
            WHERE m.needs_review=0 AND m.recipient_id IS NOT NULL AND m.processed_at IS NOT NULL
            GROUP BY m.campaign_id
        """)}
    for row in rows:
        stat = stats.get(row["id"])
        row["response_count"] = stat["response_count"] if stat else 0
        row["offer_count"] = stat["offer_count"] if stat else 0
        status = row["status"]
        row["display_status"] = ("draft" if status == "draft" else "closed" if status == "closed"
                                 else "compare" if row["offer_count"] else "waiting")
        try:
            row["deadline_label"] = date.fromisoformat(row["deadline"]).strftime("%d.%m.%Y") if row["deadline"] else ""
        except ValueError:
            row["deadline_label"] = ""
    return rows


def filtered_purchases(database, query: str, status: str):
    rows = purchase_rows(database)
    counts = {key: len(rows) if key == "all" else sum(row["display_status"] == key for row in rows) for key, _ in FILTERS}
    if status != "all":
        rows = [row for row in rows if row["display_status"] == status]
    if query:
        query = query.casefold()
        rows = [row for row in rows if query in (row["name"] + " " + row["code"]).casefold()]
    return rows, counts
