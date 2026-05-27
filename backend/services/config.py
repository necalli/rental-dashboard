import os

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.getenv("RENTAL_DATA_DIR", os.path.join(BASE_DIR, "data"))
RAW_DIR = os.getenv("RENTAL_RAW_DIR", os.path.join(BASE_DIR, "raw"))
DB_PATH = os.getenv("RENTAL_DB_PATH", os.path.join(DATA_DIR, "rental_dashboard.db"))

DEFAULT_PAGE_SIZE = int(os.getenv("RENTAL_PAGE_SIZE", "50"))
