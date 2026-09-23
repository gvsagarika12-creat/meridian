"""Meridian Behavioral Health.

Loading .env here rather than in one entry point, because there are several -
desktop.py, admin.py, and uvicorn run directly - and settings that only apply
depending on how you started the app are worse than no settings at all.
"""

from pathlib import Path

try:
    from dotenv import load_dotenv

    # override=False: a real environment variable beats the file, so a deployment
    # can set SMTP_PASSWORD properly without editing anything on disk.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    pass
