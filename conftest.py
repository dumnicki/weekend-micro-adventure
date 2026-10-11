"""Makes the project root importable so tests can `from app... import ...`.

tests/ has no __init__.py, so pytest prepends tests/ to sys.path rather than the
project root. Without this file every test that touches app.* fails at import.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))