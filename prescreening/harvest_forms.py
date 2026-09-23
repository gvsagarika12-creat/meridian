"""Reconstruct IntakeQ form definitions from completed submissions.

IntakeQ has no endpoint that returns a blank form template. `GET /questionnaires`
gives you only {Id, Name, Archived, Anonymous}. But `GET /intakes/{id}` returns a
completed submission *including its questions* — so the structure of a form can be
read back off the forms people have already filled in.

This script samples several submissions per form, unions the questions it finds,
and writes a draft YAML definition plus a report of what needs human review.

    python harvest_forms.py --dry-run          # plan only, ~55 API calls
    python harvest_forms.py                    # full run, respects the daily budget
    python harvest_forms.py --resume           # continue after hitting the cap

TWO RULES THIS SCRIPT ENFORCES
------------------------------
1. No PHI is ever written to disk. Submissions are read in memory for their question
   structure; answers are discarded. The single exception is choice-type OPTIONS —
   see `_is_choice_type` — which are aggregated across many patients and never
   associated with anyone.
2. The daily API budget is respected and progress is checkpointed, so a run that hits
   the ceiling can resume tomorrow instead of starting over.

The output is a DRAFT. Option lists are inferred from what patients happened to pick,
so they are incomplete by construction. Every choice question needs a human to check
it against the real form — especially the clinical scales, where a missing option
changes a score.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

from pull_intakeq import IntakeQ, RateLimiter

HERE = Path(__file__).resolve().parent
FORMS_DIR = HERE / "forms"
STATE_FILE = HERE / ".harvest_state.json"
REPORT_FILE = HERE / "harvest_report.md"

# Answer values are only ever kept for these question types, where the answer IS the
# option list we are trying to recover. Free text, dates and numbers are discarded.
CHOICE_TYPES = {
    "checkbox", "checkboxes", "multiplechoice", "multiple_choice", "radio",
    "dropdown", "select", "yesno", "yes_no", "scale", "rating", "matrix",
}

# IntakeQ type string -> our type. Unmapped types are reported, not silently dropped.
TYPE_MAP = {
    "text": "short_text", "shorttext": "short_text", "short_text": "short_text",
    "textarea": "long_text", "longtext": "long_text", "long_text": "long_text",
    "paragraph": "long_text",
    "number": "number", "date": "date", "datetime": "date",
    "email": "email", "phone": "phone",
    "dropdown": "dropdown", "select": "dropdown",
    "radio": "radio", "multiplechoice": "radio", "multiple_choice": "radio",
    "checkbox": "checkbox", "checkboxes": "checkbox",
    "yesno": "yes_no", "yes_no": "yes_no",
    "scale": "scale", "rating": "scale",
    "matrix": "matrix", "grid": "matrix",
    "signature": "signature", "esignature": "signature",
    "file": "file_upload", "fileupload": "file_upload", "attachment": "file_upload",
    "heading": "heading", "label": "heading", "html": "rich_text",
    "address": "address",
}


def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (s[:maxlen].rstrip("_")) or "question"


def _norm_type(raw: object) -> tuple[str, str | None]:
    """Return (mapped_type, unmapped_raw). unmapped_raw is set when we guessed."""
    key = re.sub(r"[^a-z]", "", str(raw or "").lower())
    if key in TYPE_MAP:
        return TYPE_MAP[key], None
    return "short_text", str(raw or "unknown")


def _is_choice_type(raw: object) -> bool:
    return re.sub(r"[^a-z]", "", str(raw or "").lower()) in CHOICE_TYPES


class FormDraft:
    """Accumulates question structure for one questionnaire across many submissions."""

    def __init__(self, form_id: str, name: str) -> None:
        self.form_id = form_id
        self.name = name
        self.samples = 0
        self.questions: dict[str, dict] = {}   # normalized text -> question record
        self.order_votes: dict[str, Counter] = defaultdict(Counter)
        self.unmapped_types: set[str] = set()

    def absorb(self, intake: dict) -> None:
        self.samples += 1
        for position, q in enumerate(intake.get("Questions") or []):
            text = (q.get("Text") or "").strip()
            if not text:
                continue
            key = re.sub(r"\s+", " ", text.lower())
            raw_type = q.get("Type") or q.get("QuestionType")
            mapped, unmapped = _norm_type(raw_type)
            if unmapped:
                self.unmapped_types.add(unmapped)

            record = self.questions.setdefault(
                key,
                {"text": text, "type": mapped, "raw_type": str(raw_type), "seen": 0,
                 "options": set()},
            )
            record["seen"] += 1
            self.order_votes[key][position] += 1

            # PHI GATE: only choice-type answers are retained, because there the answer
            # IS the option we are trying to recover. Everything else is dropped.
            if _is_choice_type(raw_type):
                answer = q.get("Answer")
                values = answer if isinstance(answer, list) else [answer]
                for v in values:
                    if v not in (None, "", []) and len(str(v)) <= 120:
                        record["options"].add(str(v).strip())

    def to_yaml(self) -> dict:
        ordered = sorted(
            self.questions.items(),
            key=lambda kv: self.order_votes[kv[0]].most_common(1)[0][0],
        )
        used: set[str] = set()
        questions = []
        for key, rec in ordered:
            qid = slugify(rec["text"])
            n = 2
            while qid in used:
                qid, n = f"{slugify(rec['text'], 44)}_{n}", n + 1
            used.add(qid)

            entry: dict = {"id": qid, "text": rec["text"], "type": rec["type"]}
            if rec["options"]:
                entry["options"] = sorted(rec["options"])
                entry["_options_incomplete"] = True
            if rec["seen"] < self.samples:
                entry["_conditional"] = (
                    f"appeared in {rec['seen']} of {self.samples} sampled submissions "
                    "- likely shown only under some condition"
                )
            questions.append(entry)

        return {
            "name": self.name,
            "source": {
                "system": "IntakeQ",
                "questionnaire_id": self.form_id,
                "harvested": date.today().isoformat(),
                "samples": self.samples,
            },
            "_review_required": True,
            "questions": questions,
        }


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done_intakes": [], "drafts": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _form_key(summary: dict) -> tuple[str, str]:
    """(id, name) of the questionnaire this submission belongs to, defensively."""
    name = (
        summary.get("QuestionnaireName")
        or summary.get("TemplateName")
        or summary.get("Name")
        or "Unknown form"
    )
    fid = str(summary.get("QuestionnaireId") or summary.get("TemplateId") or slugify(name))
    return fid, name


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365, help="how far back to look for submissions")
    ap.add_argument("--samples-per-form", type=int, default=8,
                    help="submissions to sample per form; more catches more conditional branches")
    ap.add_argument("--max-calls", type=int, default=400,
                    help="hard ceiling on API calls this run (daily limit is 500)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list forms and submission counts only; spends ~55 calls")
    ap.add_argument("--resume", action="store_true", help="continue a previous run")
    args = ap.parse_args()

    client = IntakeQ(os.environ.get("INTAKEQ_API_KEY", ""))
    client.limiter = RateLimiter(per_minute=8, per_day=args.max_calls)

    print("Listing questionnaire templates …", file=sys.stderr)
    templates = client._get("questionnaires") or []
    print(f"  {len(templates)} templates on the account", file=sys.stderr)

    end = date.today()
    start = end - timedelta(days=args.days)
    print(f"Listing submissions {start} → {end} …", file=sys.stderr)
    summaries = client.intake_summaries(start, end)
    print(f"  {len(summaries)} submissions found", file=sys.stderr)

    by_form: dict[str, list[dict]] = defaultdict(list)
    names: dict[str, str] = {}
    for s in summaries:
        fid, name = _form_key(s)
        names[fid] = name
        by_form[fid].append(s)

    covered = {t.get("Name") for t in templates if t.get("Name") in names.values()}
    orphans = [t.get("Name") for t in templates if t.get("Name") not in names.values()]

    print("\n--- PLAN ---", file=sys.stderr)
    planned = 0
    for fid, subs in sorted(by_form.items(), key=lambda kv: -len(kv[1])):
        take = min(len(subs), args.samples_per_form)
        planned += take
        print(f"  {names[fid][:46]:48} {len(subs):4} submissions → sample {take}", file=sys.stderr)
    print(f"\n  forms with submissions : {len(by_form)}", file=sys.stderr)
    print(f"  forms with NONE        : {len(orphans)}  (cannot be recovered)", file=sys.stderr)
    print(f"  detail calls planned   : {planned}", file=sys.stderr)

    if args.dry_run:
        print("\nDry run — nothing fetched, nothing written.", file=sys.stderr)
        if orphans:
            print("\nForms with no submissions (rebuild by hand):", file=sys.stderr)
            for n in orphans:
                print(f"  - {n}", file=sys.stderr)
        return

    state = load_state() if args.resume else {"done_intakes": [], "drafts": {}}
    done = set(state["done_intakes"])
    drafts: dict[str, FormDraft] = {}

    FORMS_DIR.mkdir(exist_ok=True)
    stopped_early = False

    for fid, subs in by_form.items():
        draft = FormDraft(fid, names[fid])
        drafts[fid] = draft
        for summary in subs[: args.samples_per_form]:
            iid = summary.get("Id")
            if not iid or iid in done:
                continue
            try:
                draft.absorb(client.intake(iid))
                done.add(iid)
            except RuntimeError as exc:          # daily budget reached
                print(f"\n{exc}", file=sys.stderr)
                stopped_early = True
                break
            except Exception as exc:             # one bad submission must not kill the run
                print(f"  ! {names[fid][:40]}: {exc}", file=sys.stderr)
        print(f"  harvested {draft.samples:2} → {names[fid][:50]}", file=sys.stderr)
        if stopped_early:
            break

    state["done_intakes"] = sorted(done)
    save_state(state)

    written = []
    for fid, draft in drafts.items():
        if draft.samples == 0:
            continue
        path = FORMS_DIR / f"{slugify(draft.name)}.yaml"
        path.write_text(
            yaml.safe_dump(draft.to_yaml(), sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        written.append((draft, path))

    lines = [
        "# Form harvest report",
        "",
        f"Run {date.today().isoformat()} · {len(written)} drafts written to `forms/`",
        "",
        "**Every draft needs a human pass.** Option lists come from what patients happened",
        "to pick, so they are incomplete by construction. A missing option in a clinical",
        "scale changes the score.",
        "",
        "## Drafts",
        "",
        "| Form | Samples | Questions | Conditional | Options to verify |",
        "|---|---|---|---|---|",
    ]
    for draft, path in sorted(written, key=lambda d: d[0].name):
        data = draft.to_yaml()
        qs = data["questions"]
        cond = sum(1 for q in qs if "_conditional" in q)
        opts = sum(1 for q in qs if q.get("_options_incomplete"))
        lines.append(f"| `{path.name}` | {draft.samples} | {len(qs)} | {cond} | {opts} |")

    if orphans:
        lines += ["", "## Not recoverable — no submissions found", "",
                  "These must be rebuilt by hand:", ""]
        lines += [f"- {n}" for n in orphans]

    unmapped = sorted({t for d in drafts.values() for t in d.unmapped_types})
    if unmapped:
        lines += ["", "## Unrecognised question types", "",
                  "Guessed as `short_text`. Check these against the real form:", ""]
        lines += [f"- `{t}`" for t in unmapped]

    if stopped_early:
        lines += ["", "## Incomplete run", "",
                  "The daily API budget was reached. Re-run tomorrow with `--resume`."]

    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(written)} drafts → forms/   ·   report → {REPORT_FILE.name}", file=sys.stderr)
    if stopped_early:
        print("Budget reached. Re-run tomorrow: python harvest_forms.py --resume", file=sys.stderr)


if __name__ == "__main__":
    main()
