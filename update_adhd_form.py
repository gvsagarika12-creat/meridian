"""Bring the live ADHD form in line with app/packets_adhd.py, without losing answers.

    python update_adhd_form.py            # show what would change
    python update_adhd_form.py --apply

install_adhd_form.py rebuilds the form by deleting its questions. That is fine
on a form nobody has filled in, and wrong the moment somebody has: `answers`
carries a foreign key to `questions`, so deleting a question either takes the
answers with it or is refused outright.

This script reconciles instead. Questions are matched by their text and left in
place; only their item lists change. Deleting an item is safe in a way deleting
a question is not - answers are stored as a mapping keyed by label, so an item
that goes away leaves its old answers intact and simply stops being asked.
"""

from __future__ import annotations

import sys

from app.models import Form, QuestionItem, SessionLocal, Submission
from app.packets_adhd import ADHD_PACKET

FORM_NAME = "ADHD in Adults Questionnaire"


def reconcile(apply: bool) -> None:
    db = SessionLocal()
    form = db.query(Form).filter_by(name=FORM_NAME).first()
    if not form:
        raise SystemExit(f"No form named {FORM_NAME!r}. Run install_adhd_form.py first.")

    wanted = {text: items for text, _t, _r, _p, _o, _rows, items in ADHD_PACKET}
    filled = db.query(Submission).filter_by(form_id=form.id).filter(
        Submission.answers.any()).count()
    changed = False

    for q in form.questions:
        spec = wanted.get(q.text)
        if spec is None:
            print(f"  ? {q.text[:56]!r} is on the form but not in the packet - left alone")
            continue

        have = {it.label: it for it in q.items}
        want = [s[0] for s in spec]

        adding = [lbl for lbl in want if lbl not in have]
        removing = [lbl for lbl in have if lbl not in want]
        if not adding and not removing:
            continue

        changed = True
        print(f"\n  {q.text[:60]}")
        for lbl in adding:
            print(f"      + {lbl}")
        for lbl in removing:
            print(f"      - {lbl}")

        if not apply:
            continue

        for lbl in removing:
            db.delete(have[lbl])
        db.flush()
        # Rewrite positions from the packet so the fields read in table order
        # rather than in the order edits happened to be made.
        for pos, s in enumerate(spec):
            label, kind, opts, width = s[:4]
            item = have.get(label)
            if item is None:
                item = QuestionItem(question_id=q.id, label=label, kind=kind,
                                    options_raw=opts, width=width,
                                    required=s[4] if len(s) > 4 else False)
                db.add(item)
            item.position = pos
            item.kind = kind
            item.options_raw = opts
            item.width = width

    if not changed:
        print("Already in line with the packet. Nothing to do.")
        return

    if not apply:
        print(f"\nPreview only. {filled} submission(s) carry answers on this form; "
              f"none would be touched.\nRe-run with --apply to make the change.")
        return

    form.version += 1
    db.commit()
    print(f"\napplied; form is now version {form.version}")
    print(f"{filled} submission(s) with answers were not touched")


if __name__ == "__main__":
    reconcile("--apply" in sys.argv)
