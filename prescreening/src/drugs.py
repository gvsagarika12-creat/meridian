"""Drug name normalisation and class lookup.

Deliberately offline and table-driven. The tables live in `data/drug_classes.yaml`
so a pharmacist can review them without reading Python — which matters, because a
wrong entry in the MAOI list is a patient-safety defect, not a bug.

Optional RxNav enrichment is available for names the table does not recognise. It is
off by default. It sends only a drug name — no patient data — but it is a network call
during screening, so turn it on deliberately.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parent.parent / "data" / "drug_classes.yaml"

# Strip dose, form and route so "Sertraline HCl 100mg tab PO" matches "sertraline".
_NOISE = re.compile(
    r"\b("
    r"\d+(\.\d+)?\s*(mg|mcg|g|ml|units?)"
    r"|hcl|hydrochloride|maleate|succinate|besylate|sodium|potassium|tartrate"
    r"|tab(let)?s?|cap(sule)?s?|er|xr|sr|cr|odt|soln|solution|susp(ension)?"
    r"|po|oral(ly)?|daily|bid|tid|qid|qhs|prn"
    r")\b",
    re.IGNORECASE,
)


def normalize(name: str) -> str:
    """Reduce a chart medication string to a comparable base name."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = _NOISE.sub(" ", text.lower())
    text = re.sub(r"[^a-z\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@functools.lru_cache(maxsize=1)
def _tables() -> dict[str, dict]:
    if not DATA.exists():
        raise FileNotFoundError(
            f"drug class table missing at {DATA}. It ships with the project; "
            "if it was removed, restore it before screening anything."
        )
    with DATA.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, set[str]]:
    """Map every known generic and brand name to the classes it belongs to."""
    idx: dict[str, set[str]] = {}
    for class_name, spec in _tables().get("classes", {}).items():
        for entry in spec.get("drugs", []):
            names = [entry["generic"]] + list(entry.get("brands", []))
            for raw in names:
                idx.setdefault(normalize(raw), set()).add(class_name)
    return idx


def classes_for(name: str) -> set[str]:
    """Classes this medication belongs to. Empty set means 'not in the table'."""
    key = normalize(name)
    if not key:
        return set()
    if key in _index():
        return set(_index()[key])
    # Combination products: "bupropion-naltrexone" or "amitriptyline/perphenazine".
    found: set[str] = set()
    for part in re.split(r"[-/+]| and ", key):
        part = part.strip()
        if part and part in _index():
            found |= _index()[part]
    return found


def in_class(name: str, class_name: str) -> bool:
    return class_name in classes_for(name)


def is_known(name: str) -> bool:
    """False means the table has never seen this drug.

    The caller must treat that as 'needs verification', not as 'not prohibited'.
    An unrecognised drug is the single most likely way this pipeline gets a
    screening decision wrong.
    """
    return bool(classes_for(name))


def known_classes() -> list[str]:
    return sorted(_tables().get("classes", {}))


def table_version() -> str:
    return str(_tables().get("version", "unversioned"))
