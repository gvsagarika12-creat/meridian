"""Vercel entry point.

Vercel routes every request here through a rewrite in vercel.json, so the app is
mounted at /api/index and that prefix arrives in the request path. _StripMount
in app/main.py removes it before anything else sees it - see the note there for
why the loop happens without it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app as _fastapi  # noqa: E402
from app.main import _StripMount  # noqa: E402

app = _StripMount(_fastapi)
