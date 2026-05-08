import os
import re
import sys
import tempfile
from pathlib import Path
import psycopg2
import config
from fetch_opinions import get_videos_from_channel
from caption_downloader import download_captions, MembersOnlyError


_URL_RE = re.compile(r'https?://')
_HASHTAG_RE = re.compile(r'#\w+')
_MAX_TRANSCRIPT_RETRIES = 5


def is_description_only(transcript: str) -> bool:
    """Return True if the stored transcript looks like a YouTube description, not real captions."""
    url_count = len(_URL_RE.findall(transcript))
    hashtag_count = len(_HASHTAG_RE.findall(transcript))
    return url_count >= 2 or hashtag_count >= 3


def main():
    conn = psycopg2.connect(config.DATABASE_URL)

    try:
        channels = get_active_channels(conn)
        print(f"Found {len(channels)} active channels")

        for channel_id, last_fetched, min_duration_seconds in channels:
            print(f"Processing channel: {channel_id}")
            process_channel(conn, channel_id, last_fetched, min_duration_seconds)

        retry_description_only_transcripts(conn)

    finally:
        conn.close()


def get_active_channels(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT c.channel_id, c.last_fetched,
                   MIN(cs.min_duration_seconds) as min_duration_seconds
            FROM channels c
            JOIN channel_subscriptions cs ON c.channel_id = cs.channel_id
            WHERE cs.is_active = TRUE
              AND cs.is_paused_by_downgrade = FALSE
            GROUP BY c.channel_id, c.last_fetched
        """)
        return cur.fetchall()


def process_channel(conn, channel_id, last_fetched, min_duration_seconds):
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    last_fetched_str = last_fetched.isoformat() if last_fetched else None

    videos = get_videos_from_channel(channel_url, last_fetched_str, min_duration_seconds)

    if not videos:
        print(f"  No new videos for {channel_id}")
        return

    print(f"  Found {len(videos)} new videos")

    with tempfile.TemporaryDirectory() as tmp_dir:
        for video in videos:
            process_video(conn, channel_id, video, tmp_dir)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE channels SET last_fetched = NOW() WHERE channel_id = %s",
            (channel_id,)
        )
    conn.commit()
    print(f"  Updated last_fetched for {channel_id}")


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
        print(f"    Transcript fallback (title+desc): {e}")
        transcript_text = f"{video['title']}\n\n{video.get('description', '')}"

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


def retry_description_only_transcripts(conn) -> None:
    """Re-fetch captions for videos whose stored transcript is a description fallback.

    Runs after new-video ingestion. For each video with a description-only transcript
    and fewer than _MAX_TRANSCRIPT_RETRIES attempts, tries to download real captions.
    On success: overwrites transcript_compressed and resets the counter.
    On failure: increments transcript_fetch_attempts (summarizer will give up at >= 5).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT video_id, transcript_compressed
            FROM videos
            WHERE transcript_fetch_attempts < %s
        """, (_MAX_TRANSCRIPT_RETRIES,))
        rows = cur.fetchall()

    candidates = [(vid_id, t) for vid_id, t in rows if is_description_only(t)]
    if not candidates:
        return

    print(f"Retrying caption download for {len(candidates)} description-only videos")
    with tempfile.TemporaryDirectory() as tmp_dir:
        for video_id, _ in candidates:
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
                print(f"  Skipped {video_id}: members-only content")
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
