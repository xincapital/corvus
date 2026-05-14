import os
import json
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

_raw_yt = os.environ.get("YOUTUBE_DATA_API_KEYS", os.environ.get("YOUTUBE_DATA_API_KEY", ""))
YOUTUBE_DATA_API_KEYS: list[str] = [k.strip() for k in _raw_yt.split(",") if k.strip()]
YOUTUBE_DATA_API_KEY = YOUTUBE_DATA_API_KEYS[0] if YOUTUBE_DATA_API_KEYS else ""

_raw = os.environ.get("SUPADATA_KEYS", os.environ.get("SUPADATA_KEY", ""))
SUPADATA_KEYS: list[str] = json.loads(_raw) if _raw.startswith("[") else ([_raw] if _raw else [])
