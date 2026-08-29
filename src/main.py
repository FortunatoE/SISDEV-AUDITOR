"""Secure local/process entry point for the Flask application."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.index import app

if __name__ == "__main__":
    app.run(
        host=os.getenv("SISDEV_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8765")),
        debug=False,
    )
