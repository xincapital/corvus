import re
import isodate
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from googleapiclient.discovery import build

import config


def _youtube_client():
    return build('youtube', 'v3', developerKey=config.YOUTUBE_DATA_API_KEY)


def _resolve_channel_id(youtube, channel_url: str) -> Optional[str]:
    handle_match = re.search(r'@([\w.-]+)', channel_url)
    if handle_match:
        handle = handle_match.group(1)
        resp = youtube.channels().list(part='id', forHandle=handle).execute()
        items = resp.get('items', [])
        if items:
            return items[0]['id']

    id_match = re.search(r'/channel/(UC[\w-]+)', channel_url)
    if id_match:
        return id_match.group(1)

    user_match = re.search(r'/user/([\w-]+)', channel_url)
    if user_match:
        resp = youtube.channels().list(part='id', forUsername=user_match.group(1)).execute()
        items = resp.get('items', [])
        if items:
            return items[0]['id']

    return None


def get_videos_from_channel(
    channel_url: str,
    last_fetched: Optional[str],
    min_duration_seconds: int = 600,
) -> List[Dict[str, Any]]:
    videos = []

    try:
        youtube = _youtube_client()

        channel_id = _resolve_channel_id(youtube, channel_url)
        if not channel_id:
            print(f"Could not resolve channel ID for: {channel_url}")
            return videos

        uploads_playlist_id = 'UU' + channel_id[2:]

        page_token = None
        collected_ids = []
        cutoff = datetime.fromisoformat(last_fetched) if last_fetched else None
        stop_paging = False

        while not stop_paging:
            req = youtube.playlistItems().list(
                part='contentDetails,snippet',
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=page_token
            )
            resp = req.execute()

            for item in resp.get('items', []):
                snippet = item['snippet']
                video_id = item['contentDetails']['videoId']
                published_str = snippet.get('publishedAt', '')

                if not published_str:
                    if not last_fetched:
                        collected_ids.append((video_id, datetime.now(timezone.utc)))
                    continue

                published_at = datetime.fromisoformat(published_str.replace('Z', '+00:00'))

                if cutoff and published_at <= cutoff:
                    stop_paging = True
                    break

                collected_ids.append((video_id, published_at))

            page_token = resp.get('nextPageToken')
            if not page_token:
                break

            if not last_fetched:
                break

        if not collected_ids:
            return videos

        for batch_start in range(0, len(collected_ids), 50):
            batch = collected_ids[batch_start:batch_start + 50]
            ids_str = ','.join(vid_id for vid_id, _ in batch)
            vid_resp = youtube.videos().list(
                part='contentDetails,liveStreamingDetails,snippet',
                id=ids_str
            ).execute()

            meta = {v['id']: v for v in vid_resp.get('items', [])}

            for video_id, published_at in batch:
                v = meta.get(video_id)
                if not v:
                    continue

                duration_iso = v['contentDetails'].get('duration', 'PT0S')
                duration_s = int(isodate.parse_duration(duration_iso).total_seconds())

                if duration_s == 0:
                    continue

                if duration_s < min_duration_seconds:
                    continue

                url = f"https://www.youtube.com/watch?v={video_id}"
                videos.append({
                    'video_id': video_id,
                    'title': v['snippet']['title'],
                    'duration': duration_s,
                    'published_at': published_at.isoformat(),
                    'url': url,
                    'description': v['snippet'].get('description', ''),
                })

                if not last_fetched:
                    print(f"Found first video for new channel: {v['snippet']['title'][:50]}")
                    return videos

        return videos

    except Exception as e:
        print(f"Error fetching videos: {e}")
        return videos
