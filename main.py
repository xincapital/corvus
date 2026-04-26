import os
import sys
import tempfile
import psycopg2
import config
from fetch_opinions import get_videos_from_channel
from caption_downloader import download_captions, MembersOnlyError


def main():
    conn = psycopg2.connect(config.DATABASE_URL)

    try:
        channels = get_active_channels(conn)
        print(f"Found {len(channels)} active channels")

        for channel_id, last_fetched, min_duration_seconds in channels:
            print(f"Processing channel: {channel_id}")
            process_channel(conn, channel_id, last_fetched, min_duration_seconds)

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
        result = download_captions(video['url'], tmp_dir)
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


if __name__ == "__main__":
    main()
