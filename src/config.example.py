"""Public-distribution defaults. Mutable state is stored through ranker_data_layout.py."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent
ARTIST_TAGS_FILE = ROOT / "danbooru_artist_tags_v4.5.txt"
ELO_RATINGS_FILE = ROOT / "artist_elo_ratings.json"
COMPARISON_IMAGES_DIR = ROOT / "comparison_images"
COMPARISON_HISTORY_FILE = ROOT / "comparison_history.json"
ACTIVE_POOL_FILE = ROOT / "active_pool.json"
STEPS = 28
IMG_WIDTH = 832
IMG_HEIGHT = 1216
DEFAULT_ELO = 1500.0
K_FACTOR = 32.0
ACTIVE_POOL_SIZE = 150
NEW_ARTIST_PROBABILITY = 0.20
LOSER_ROTATION_PROBABILITY = 0.35
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("ARTIST_ELO_SERVER_PORT", "7860"))
NEGATIVE_PROMPT = "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality"
DEFAULT_PROMPT = "masterpiece, best quality"
FRESH_DISCOVERY_QUOTA_WEIGHT = 0.50
UI_THEME_DEFAULT = "System"
UI_CUSTOM_THEME_MODE = "dark"
