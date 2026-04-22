import os
import json
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
YOUTUBE_DATA_API_KEY = os.environ["YOUTUBE_DATA_API_KEY"]

_raw = os.environ.get("SUPADATA_KEYS", os.environ.get("SUPADATA_KEY", ""))
SUPADATA_KEYS: list[str] = json.loads(_raw) if _raw.startswith("[") else ([_raw] if _raw else [])
