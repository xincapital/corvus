import os
import sys
import tempfile
from pathlib import Path
import psycopg2
import config
from fetch_opinions import get_videos_from_channel
from caption_downloader import download_captions, MembersOnlyError


_MAX_TRANSCRIPT_RETRIES = 5
# Total videos processed per run (new + retries combined). Prevents timeout on
# large backlogs. Override via MAX_VIDEOS_PER_RUN env var (0 = unlimited).
_MAX_VIDEOS_PER_RUN = int(os.environ.get("MAX_VIDEOS_PER_RUN", "150"))


def main():
    conn = psycopg2.connect(config.DATABASE_URL)
    budget = _MAX_VIDEOS_PER_RUN  # remaining slots; None when unlimited

    try:
        channels = get_active_channels(conn)
        print(f"Found {len(channels)} active channels")

        for channel_id, last_fetched in channels:
            if budget is not None and budget <= 0:
                print(f"Video budget exhausted ({_MAX_VIDEOS_PER_RUN}), stopping new-video ingestion")
                break
            print(f"Processing channel: {channel_id}")
            processed = process_channel(conn, channel_id, last_fetched, limit=budget)
            if budget is not None:
                budget -= processed

        retry_limit = budget if (budget is not None and budget > 0) else (0 if budget == 0 else None)
        retry_null_transcripts(conn, limit=retry_limit)

    finally:
        conn.close()


def get_active_channels(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT c.channel_id, c.last_fetched
            FROM channels c
            WHERE c.is_blog_source = TRUE
               OR EXISTS (
                   SELECT 1 FROM channel_subscriptions cs
                   WHERE cs.channel_id = c.channel_id
                     AND cs.is_active = TRUE
                     AND cs.is_paused_by_downgrade = FALSE
               )
        """)
        return cur.fetchall()


def process_channel(conn, channel_id, last_fetched, limit=None) -> int:
    """Process new videos for a channel. Returns count of videos processed."""
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    last_fetched_str = last_fetched.isoformat() if last_fetched else None

    videos = get_videos_from_channel(channel_url, last_fetched_str)

    if not videos:
        print(f"  No new videos for {channel_id}")
        return 0

    if limit is not None:
        videos = videos[:limit]

    print(f"  Found {len(videos)} new videos")

    with tempfile.TemporaryDirectory() as tmp_dir:
        for video in videos:
            process_video(conn, channel_id, video, tmp_dir)
            # Update last_fetched after each video so a mid-channel timeout
            # doesn't cause the whole channel to be reprocessed next run.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE channels SET last_fetched = %s WHERE channel_id = %s",
                    (video['published_at'], channel_id),
                )
            conn.commit()

    print(f"  Updated last_fetched for {channel_id}")
    return len(videos)


def process_video(conn, channel_id, video, tmp_dir):
    video_id = video['video_id']
    print(f"  Processing video: {video['title'][:60]}")

    try:
        result = download_captions(video['url'], Path(tmp_dir))
        with open(result['caption_file'], encoding='utf-8') as fh:
            transcript_text = fh.read()
    except MembersOnlyError:
        print(f"    Skipped (members-only): {video_id}")
        return
    except Exception as e:
        print(f"    Caption download failed, storing NULL transcript: {e}", file=sys.stderr)
        transcript_text = None

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO videos (video_id, channel_id, title, description,
                                published_at, duration_seconds, transcript_compressed, view_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (video_id) DO NOTHING
        """, (
            video_id,
            channel_id,
            video['title'],
            video.get('description'),
            video['published_at'],
            video['duration'],
            transcript_text,
            video.get('view_count'),
        ))
    conn.commit()


def retry_null_transcripts(conn, limit=None) -> None:
    """Re-fetch captions for videos with NULL transcript_compressed.

    Runs after new-video ingestion. For each video with a NULL transcript
    and fewer than _MAX_TRANSCRIPT_RETRIES attempts, tries to download real captions.
    On success: overwrites transcript_compressed and resets the counter.
    On failure: increments transcript_fetch_attempts.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT video_id
            FROM videos
            WHERE transcript_compressed IS NULL AND transcript_fetch_attempts < %s
        """, (_MAX_TRANSCRIPT_RETRIES,))
        rows = cur.fetchall()

    candidates = [vid_id for (vid_id,) in rows]
    if not candidates:
        return

    if limit is not None:
        if limit <= 0:
            print(f"Video budget exhausted, skipping retry of {len(candidates)} NULL transcripts")
            return
        candidates = candidates[:limit]

    print(f"Retrying caption download for {len(candidates)} videos with NULL transcript")
    with tempfile.TemporaryDirectory() as tmp_dir:
        for video_id in candidates:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                result = download_captions(video_url, Path(tmp_dir))
                with open(result['caption_file'], encoding='utf-8') as fh:
                    transcript_text = fh.read()
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE videos
                        SET transcript_compressed = %s, transcript_fetch_attempts = 0
                        WHERE video_id = %s
                    """, (transcript_text, video_id))
                conn.commit()
                print(f"  Re-fetched transcript for {video_id} ({len(transcript_text)} chars)")
            except MembersOnlyError:
                with conn.cursor() as cur:
                    cur.execute("UPDATE videos SET transcript_fetch_attempts = 5 WHERE video_id = %s", (video_id,))
                conn.commit()
                print(f"  {video_id}: members-only, permanently skipped (attempts=5)")
            except Exception as e:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE videos
                        SET transcript_fetch_attempts = transcript_fetch_attempts + 1
                        WHERE video_id = %s
                    """, (video_id,))
                conn.commit()
                print(f"  Re-fetch failed for {video_id} (attempts incremented): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
