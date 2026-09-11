import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['TEMP'] = str(ROOT / '.tmp')
os.environ['TMP'] = str(ROOT / '.tmp')
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
