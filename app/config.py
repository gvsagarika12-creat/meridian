"""Practice branding, loaded from a config file kept out of version control.

`config/practice.json` holds the real details and is gitignored.
`config/practice.example.json` is the committed template with placeholders.

If the real file is absent the app still runs - it falls back to the example and
says so - so a fresh clone works without anyone hunting for missing config.

The values here are public business contact details (the letterhead printed on
patient-facing forms), not PHI. They live outside the repo because a practice's
identity does not belong in source control, not because they are sensitive.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REAL = CONFIG_DIR / "practice.json"
EXAMPLE = CONFIG_DIR / "practice.example.json"

DEFAULTS = {
    "name": "Your Practice Name",
    # What goes in the top bar. The legal name belongs on a letterhead, not in a
    # nav bar next to nine menu items - so a practice can set a shorter one.
    # Falls back to `name` in the templates when it is left empty.
    "short_name": "",
    "address": "Street, City, ST 00000",
    "phone": "(000) 000-0000",
    "fax": "(000) 000-0000",
    "website": "www.example.com",
    "logo_letters": ["Y", "P", "N", "?"],
    "logo_colours": ["#5ec9c4", "#4aa3d8", "#2f7fc4", "#16406b"],
}

_warned = False


def load() -> dict:
    """Real config if present, else the example template, else built-in defaults."""
    global _warned
    for path in (REAL, EXAMPLE):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"! {path.name} is not valid JSON ({exc}). Using defaults.\n")
                break
            if path is EXAMPLE and not _warned:
                sys.stderr.write(
                    f"! {REAL.name} not found - using placeholder branding from "
                    f"{EXAMPLE.name}.\n  Copy it to {REAL.name} and fill in your details.\n"
                )
                _warned = True
            return {**DEFAULTS, **data}
    return dict(DEFAULTS)
