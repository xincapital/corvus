"""Blog translation completeness and pipeline freshness monitor.

Provides validation functions to monitor database completeness:
  - scan_all_incomplete(conn, expected_locales=11) -> list[dict]
  - check_video_ingestion_freshness(conn, max_stale_hours=48) -> bool
"""
import sys

import psycopg2
import psycopg2.extras


def scan_all_incomplete(conn, expected_locales: int = 11) -> list:
    """Scan ALL published blog_posts and return slugs with fewer than expected_locales rows.

    Returns list of dicts with keys:
      - slug: str
      - existing_locales: list of locale codes already present
      - missing_count: int (expected_locales - actual count)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                slug,
                array_agg(locale ORDER BY locale) AS existing_locales,
                %s - COUNT(DISTINCT locale) AS missing_count
            FROM blog_posts
            WHERE is_published = TRUE
            GROUP BY slug
            HAVING COUNT(DISTINCT locale) < %s
            ORDER BY missing_count DESC, slug
            """,
            (expected_locales, expected_locales),
        )
        rows = cur.fetchall()


    return [dict(r) for r in rows]


def check_video_ingestion_freshness(conn, max_stale_hours: int = 48) -> bool:
    """Check if videos have been ingested within the last max_stale_hours.

    Queries MAX(created_at) from videos.
    If stale or empty, logs a warning to stderr and returns False.
    Returns True if fresh videos exist.
    """
    from datetime import datetime, timezone, timedelta

    with conn.cursor() as cur:
        cur.execute("SELECT MAX(created_at) FROM videos;")
        row = cur.fetchone()
        latest = row[0] if row else None

    if latest is None:
        print(
            "[pipeline-monitor] WARNING: No videos found in videos table! "
            "Pipeline may be uninitialized or stalled.",
            file=sys.stderr,
        )
        return False

    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    age = now - latest
    stale_delta = timedelta(hours=max_stale_hours)

    if age > stale_delta:
        hours_ago = int(age.total_seconds() // 3600)
        print(
            f"[pipeline-monitor] WARNING: Upstream pipeline may be stalled! "
            f"Latest video ingested was {hours_ago} hours ago (threshold: {max_stale_hours}h).",
            file=sys.stderr,
        )
        return False

    print(f"[pipeline-monitor] Video ingestion is fresh (latest: {latest.isoformat()}). OK.")
    return True


def main() -> None:
    """Standalone entry point: scan DB and print structured report."""
    import config

    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        check_video_ingestion_freshness(conn)
        incomplete = scan_all_incomplete(conn)
        if not incomplete:
            print("[blog-monitor] All published blog posts have 11 locales. OK.")
        else:
            print(
                f"[blog-monitor] {len(incomplete)} slug(s) have fewer than 11 locales:",
                file=sys.stderr,
            )
            for row in incomplete:
                print(
                    f"  slug={row['slug']} "
                    f"have={row['existing_locales']} "
                    f"missing={row['missing_count']}",
                    file=sys.stderr,
                )
            print(
                f"\n[blog-monitor] Incomplete Slugs: {[r['slug'] for r in incomplete]}"
            )
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
