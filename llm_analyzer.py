from typing import Dict, Any
import sys

try:
    from config import nvidia_chat
except ImportError:
    nvidia_chat = None


def analyze_captions(caption_file_path: str, video_info: Dict[str, Any]) -> str:
    if nvidia_chat is None:
        raise RuntimeError("LLM not configured")

    video_url = video_info.get('video_url', '')

    print(f"Reading SRT file: {caption_file_path}", file=sys.stderr)
    with open(caption_file_path, 'r', encoding='utf-8') as f:
        srt_content = f.read()

    print(f"SRT content length: {len(srt_content)} characters", file=sys.stderr)

    prompt = f"""视频标题: {video_info.get('video_title', 'Unknown')}
视频链接: {video_url}
时长: {video_info.get('duration', 0)} 秒
上传者: {video_info.get('uploader', 'Unknown')}

字幕内容（SRT格式，包含序号、时间戳和文本）:
{srt_content}

请用中文提供详细总结，按以下结构输出：

## 1. 核心论点
用2-3句话说清楚：这期视频的**核心主张**是什么？主持人试图论证什么？各个话题之间有什么内在逻辑关联？

## 2. 内容结构（时间比例）
按视频的实际时间顺序，列出各段内容及其大致时长（例如"00:00–17:00 纽约袭击事件"），让读者一眼看出视频的重点分布。

## 3. 关键要点
按**主题**分组，每组用 `### 3.X 主题标题` 作为子标题（例如"### 3.1 纽约袭击事件"）。每组内按时间顺序列出要点，**覆盖视频全程**，不得有大段空白。每个关键论点或事件必须附上时间戳链接，格式为：[HH:MM:SS]({video_url}&t=XXXs) 其中XXX是秒数。

**重要规则**:
- 输入的字幕是标准的SRT格式，包含序号、时间戳（HH:MM:SS,mmm --> HH:MM:SS,mmm）和字幕文本
- 时间戳请从SRT格式中提取，必须准确
- 第3节必须覆盖视频从头到尾，不得跳过任何超过5分钟的时间段
- 每个主题组对应视频中的一个独立话题，组数通常为4–8个，不要过细也不要过粗
- **中立性**: 严格如实呈现视频内容，不添加任何政治立场的评价或判断。不论视频的政治倾向如何（保守派、自由派、左翼、右翼），均以相同标准客观描述其内容和论点，不使用「宣传」、「偏见」、「虚假」等带定性色彩的词语。
- **人名保留原文**: 视频中出现的人名、机构名一律使用原文（如 Tucker Carlson、Elon Musk、Donald Trump），不翻译成中文。

请用清晰、结构化的 Markdown 格式回答。"""

    print("Sending request to NVIDIA LLM API...", file=sys.stderr)

    try:
        full_response = nvidia_chat(
            messages=[
                {
                    "role": "system",
                    "content": "你是一个提供详细准确视频总结的智能助手。你的总结结构清晰、见解深刻且易于理解。请用中文回答。\n\n【中立性要求】你必须保持严格的政治中立，如实呈现视频内容，不对任何政治立场、意识形态或观点添加正面或负面的价值判断。不使用「宣传」、「虚构」、「操纵」等带有定性色彩的词语来描述内容本身，除非视频主持人明确使用了这些词语。你的任务是准确描述视频说了什么，而不是评价对错。"
                },
                {"role": "user", "content": prompt},
            ],
        )
        print("Completed receiving response", file=sys.stderr)
        return full_response
    except Exception as e:
        print(f"Error calling NVIDIA LLM API: {e}", file=sys.stderr)
        raise
