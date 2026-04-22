import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
YOUTUBE_DATA_API_KEY = os.environ["YOUTUBE_DATA_API_KEY"]
SUPADATA_KEY = os.environ.get("SUPADATA_KEY", "")
