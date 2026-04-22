import re
import sys
from pathlib import Path


def parse_timestamp(timestamp_str):
    match = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', timestamp_str)
    if not match:
        return 0

    hours, minutes, seconds, milliseconds = match.groups()
    total_seconds = (
        int(hours) * 3600 +
        int(minutes) * 60 +
        int(seconds) +
        int(milliseconds) / 1000
    )
    return total_seconds


def format_timestamp(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds % 1) * 1000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def parse_srt(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n', content.strip())

    subtitles = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue

        try:
            number = int(lines[0])
            timestamp_line = lines[1]
            text = ' '.join(lines[2:])

            match = re.match(r'(\S+)\s*-->\s*(\S+)', timestamp_line)
            if match:
                start_str, end_str = match.groups()
                start_time = parse_timestamp(start_str)
                end_time = parse_timestamp(end_str)

                subtitles.append({
                    'number': number,
                    'start': start_time,
                    'end': end_time,
                    'text': text.strip()
                })
        except (ValueError, IndexError):
            continue

    return subtitles


def compress_subtitles(subtitles, max_gap=2.0, max_chars=200):
    if not subtitles:
        return []

    compressed = []
    current = {
        'start': subtitles[0]['start'],
        'end': subtitles[0]['end'],
        'text': subtitles[0]['text']
    }

    for i in range(1, len(subtitles)):
        sub = subtitles[i]
        time_gap = sub['start'] - current['end']
        combined_text = current['text'] + ' ' + sub['text']

        should_merge = (
            time_gap <= max_gap and
            len(combined_text) <= max_chars and
            not current['text'].endswith('.') and
            not current['text'].endswith('!') and
            not current['text'].endswith('?')
        )

        if should_merge:
            current['end'] = sub['end']
            current['text'] = combined_text
        else:
            compressed.append(current)
            current = {
                'start': sub['start'],
                'end': sub['end'],
                'text': sub['text']
            }

    compressed.append(current)

    return compressed


def write_srt(subtitles, output_path):
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, sub in enumerate(subtitles, 1):
            start_str = format_timestamp(sub['start'])
            end_str = format_timestamp(sub['end'])

            f.write(f"{i}\n")
            f.write(f"{start_str} --> {end_str}\n")
            f.write(f"{sub['text']}\n")
            f.write("\n")
