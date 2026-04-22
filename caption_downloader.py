import sys
import time
import random
from pathlib import Path
from typing import Dict, Any, List
import re
import httpx
import config

try:
    from supadata import Supadata, SupadataError, Transcript
    SUPADATA_AVAILABLE = True
except ImportError:
    SUPADATA_AVAILABLE = False
    print("Warning: supadata not installed. Install with: pip install supadata", file=sys.stderr)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Warning: playwright not installed. Install with: pip install playwright && playwright install chromium", file=sys.stderr)

try:
    from compress_srt import parse_srt, compress_subtitles, write_srt
except ImportError:
    try:
        from .compress_srt import parse_srt, compress_subtitles, write_srt
    except ImportError:
        parse_srt = None
        compress_subtitles = None
        write_srt = None


class MembersOnlyError(Exception):
    pass


def is_members_only_error(error_message: str) -> bool:
    members_only_indicators = [
        'members-only',
        'members only',
        'join this channel',
        'available to members',
        'membership required',
        'channel membership',
        'private video',
        'this video is private',
    ]

    error_lower = str(error_message).lower()
    return any(indicator in error_lower for indicator in members_only_indicators)


def extract_video_id(url: str) -> str:
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com\/embed\/([a-zA-Z0-9_-]{11})',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    if len(url) == 11 and re.match(r'^[a-zA-Z0-9_-]+$', url):
        return url

    raise ValueError(f"Could not extract video ID from URL: {url}")


def milliseconds_to_srt_timestamp(milliseconds: int) -> str:
    total_seconds = milliseconds / 1000.0

    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(milliseconds % 1000)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def supadata_to_srt(transcript_chunks: List[Dict[str, Any]]) -> str:
    srt_lines = []

    for i, chunk in enumerate(transcript_chunks, 1):
        text = chunk['text']
        offset = chunk['offset']
        duration = chunk['duration']

        start_time = milliseconds_to_srt_timestamp(offset)
        end_time = milliseconds_to_srt_timestamp(offset + duration)

        srt_lines.append(str(i))
        srt_lines.append(f"{start_time} --> {end_time}")
        srt_lines.append(text)
        srt_lines.append("")

    return "\n".join(srt_lines)


def download_captions_with_supadata(video_url: str, output_dir: Path) -> Dict[str, Any]:
    if not SUPADATA_AVAILABLE:
        raise ImportError("supadata is not installed. Install with: pip install supadata")

    output_dir.mkdir(parents=True, exist_ok=True)

    video_id = extract_video_id(video_url)

    print(f"Using Supadata API for video {video_id}...", file=sys.stderr)

    if not config.SUPADATA_KEYS:
        raise Exception("No Supadata API key found. Please set SUPADATA_KEYS in environment")

    try:
        api_key_used = random.choice(config.SUPADATA_KEYS)
        supadata = Supadata(api_key=api_key_used)

        transcript = supadata.transcript(
            url=video_url,
            lang="en",
            text=False,
            mode="auto"
        )

        if not isinstance(transcript, Transcript):
            job_id = transcript.job_id
            print(f"Async job {job_id[:8]}..., polling...", file=sys.stderr)
            for _ in range(60):
                time.sleep(5)
                resp = httpx.get(
                    f"https://api.supadata.ai/v1/transcript/{job_id}",
                    headers={"x-api-key": api_key_used},
                    timeout=30,
                ).json()
                status = resp.get("status")
                if status == "completed":
                    transcript = Transcript(**{k: v for k, v in resp.items() if k != "status"})
                    break
                elif status == "failed":
                    raise Exception(f"Async transcript job failed")
            else:
                raise Exception(f"Async transcript job timed out after 300s")

        content = transcript.content

        print(f"Downloaded {len(content)} transcript segments", file=sys.stderr)

        transcript_chunks = [
            {
                'text': chunk.text,
                'offset': chunk.offset,
                'duration': chunk.duration,
                'lang': chunk.lang
            }
            for chunk in content
        ]

        srt_content = supadata_to_srt(transcript_chunks)

        caption_file = output_dir / f"{video_id}.en.supadata.srt"
        with open(caption_file, 'w', encoding='utf-8') as f:
            f.write(srt_content)

        print(f"Saved SRT: {caption_file.name}", file=sys.stderr)

        if parse_srt is not None:
            try:
                subtitles = parse_srt(caption_file)
                compressed = compress_subtitles(subtitles, max_gap=2.0, max_chars=200)

                compressed_file = caption_file.parent / f"{caption_file.stem}_compressed.srt"
                write_srt(compressed, compressed_file)

                original_size = caption_file.stat().st_size
                compressed_size = compressed_file.stat().st_size
                reduction = (1 - compressed_size/original_size) * 100

                print(f"Compressed: {len(subtitles)} -> {len(compressed)} entries ({reduction:.1f}% size reduction)", file=sys.stderr)
                caption_file = compressed_file
            except Exception as e:
                print(f"Compression failed: {e}. Using original file.", file=sys.stderr)

        return {
            'video_id': video_id,
            'video_url': video_url,
            'caption_file': str(caption_file),
        }

    except SupadataError as e:
        error_msg = str(e)
        if is_members_only_error(error_msg):
            raise MembersOnlyError(f"Video is members-only content: {e}")
        raise

    except Exception as e:
        if is_members_only_error(str(e)):
            raise MembersOnlyError(f"Video is members-only content: {e}")
        raise


def _notegpt_chunks_to_srt(chunks: List[Dict[str, Any]]) -> str:
    lines = []
    for i, chunk in enumerate(chunks, 1):
        start = chunk['start'].replace('.', ',') + ',000' if ',' not in chunk['start'] else chunk['start']
        end   = chunk['end'].replace('.', ',')   + ',000' if ',' not in chunk['end']   else chunk['end']
        if len(start) == 8:
            start += ',000'
        if len(end) == 8:
            end += ',000'
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(chunk['text'].strip())
        lines.append('')
    return '\n'.join(lines)


def download_captions_with_notegpt(video_url: str, output_dir: Path) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        raise ImportError("playwright is not installed. Install with: pip install playwright && playwright install chromium")

    output_dir.mkdir(parents=True, exist_ok=True)
    video_id = extract_video_id(video_url)

    print(f"Using NoteGPT (Playwright) for video {video_id}...", file=sys.stderr)

    transcript_data = {}

    def handle_response(response):
        if 'video-transcript' in response.url and 'video_id=' in response.url:
            try:
                body = response.json()
                if body.get('code') == 100000:
                    transcript_data['result'] = body['data']
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.on("response", handle_response)

        print(f"Loading NoteGPT page...", file=sys.stderr)
        page.goto(
            "https://notegpt.io/youtube-transcript-generator",
            wait_until="networkidle",
            timeout=30000,
        )

        page.fill('input[placeholder*="YouTube"]', video_url)
        time.sleep(0.5)

        for btn in page.query_selector_all("button"):
            if "Generate Transcript" in btn.inner_text():
                btn.click()
                break

        deadline = time.time() + 30
        while time.time() < deadline and 'result' not in transcript_data:
            page.wait_for_timeout(1000)

        browser.close()

    if 'result' not in transcript_data:
        raise Exception("NoteGPT did not return a transcript within 30 seconds")

    result = transcript_data['result']

    video_info = result.get('videoInfo', {})
    if is_members_only_error(str(video_info)):
        raise MembersOnlyError(f"Video appears to be members-only or private")

    transcripts = result.get('transcripts', {})
    if not transcripts:
        raise Exception("NoteGPT returned no transcripts")

    lang = next(iter(transcripts))
    tracks = transcripts[lang]
    chunks = tracks.get('default') or tracks.get('auto') or tracks.get('custom') or []

    if not chunks:
        raise Exception(f"NoteGPT returned empty transcript for language '{lang}'")

    print(f"Got {len(chunks)} chunks (lang={lang})", file=sys.stderr)

    srt_content = _notegpt_chunks_to_srt(chunks)
    caption_file = output_dir / f"{video_id}.{lang}.notegpt.srt"
    caption_file.write_text(srt_content, encoding='utf-8')
    print(f"Saved SRT: {caption_file.name}", file=sys.stderr)

    if parse_srt is not None:
        try:
            subtitles = parse_srt(caption_file)
            compressed = compress_subtitles(subtitles, max_gap=2.0, max_chars=200)
            compressed_file = caption_file.parent / f"{caption_file.stem}_compressed.srt"
            write_srt(compressed, compressed_file)
            original_size = caption_file.stat().st_size
            compressed_size = compressed_file.stat().st_size
            reduction = (1 - compressed_size / original_size) * 100
            print(f"Compressed: {len(subtitles)} -> {len(compressed)} entries ({reduction:.1f}% size reduction)", file=sys.stderr)
            caption_file = compressed_file
        except Exception as e:
            print(f"Compression failed: {e}. Using original file.", file=sys.stderr)

    return {
        'video_id': video_id,
        'video_url': video_url,
        'caption_file': str(caption_file),
    }


def _download_with_notegpt_retries(video_url, tmp_dir, max_attempts=3, delay=5):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return download_captions_with_notegpt(video_url, tmp_dir)
        except MembersOnlyError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                time.sleep(delay)
    raise last_exc


def download_captions(video_url: str, output_dir: Path) -> Dict[str, Any]:
    errors = []

    if PLAYWRIGHT_AVAILABLE:
        try:
            return _download_with_notegpt_retries(video_url, output_dir)
        except MembersOnlyError:
            raise
        except Exception as e:
            errors.append(f"NoteGPT: {e}")
            print(f"NoteGPT failed: {e}", file=sys.stderr)
            print(f"Falling back to Supadata...", file=sys.stderr)
    else:
        errors.append("NoteGPT: playwright not available")
        print(f"Playwright not available, trying Supadata...", file=sys.stderr)

    if SUPADATA_AVAILABLE and config.SUPADATA_KEYS:
        try:
            return download_captions_with_supadata(video_url, output_dir)
        except MembersOnlyError:
            raise
        except Exception as e:
            errors.append(f"Supadata: {e}")
            print(f"Supadata failed: {e}", file=sys.stderr)
    else:
        errors.append("Supadata: not available or no API key")

    raise Exception(f"All caption download methods failed:\n" + "\n".join(f"  - {e}" for e in errors))
