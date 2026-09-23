"""Clear empty submissions off the Client Forms list.

    python tidy_submissions.py            # show what would go
    python tidy_submissions.py --apply    # actually remove them

Two kinds of row are removed, and only these two:

* **Empty rows on an archived form.** The form is out of the library, nobody
  will ever fill it, and the row carries nothing.
* **Empty duplicates.** The same patient and the same form already have a row
  with answers on it, so the empty one is a stale second request.

A submission with even one answer is never touched, whatever state it is in.
The list is allowed to look untidy; losing a patient's answers to a tidy-up is
not a trade worth making.
"""

from __future__ import annotations

import sys
from collections import defaultdict

from app.models import Form, MessageLog, SessionLocal, Submission


def plan() -> tuple[list[tuple[Submission, str]], int]:
    db = SessionLocal()
    subs = db.query(Submission).order_by(Submission.id).all()

    with_answers: dict[tuple[int, int], bool] = defaultdict(bool)
    for s in subs:
        if s.answers:
            with_answers[(s.client_id, s.form_id)] = True

    doomed: list[tuple[Submission, str]] = []
    for s in subs:
        if s.answers:
            continue
        if not s.form.is_active:
            doomed.append((s, f"empty, and {s.form.name} is archived"))
        elif with_answers[(s.client_id, s.form_id)]:
            doomed.append((s, "empty duplicate - a completed one already exists"))
    return doomed, len(subs)


def apply(doomed: list[tuple[Submission, str]]) -> int:
    db = SessionLocal()
    removed = 0
    for sub, _why in doomed:
        row = db.get(Submission, sub.id)
        if row is None or row.answers:          # re-check against the live row
            continue
        # The outbox keeps a record of every message sent about a submission,
        # and the database will not let the submission go while it does.
        db.query(MessageLog).filter_by(submission_id=row.id).delete()
        db.delete(row)
        removed += 1
    db.commit()
    return removed


if __name__ == "__main__":
    doomed, total = plan()
    if not doomed:
        print(f"Nothing to remove. {total} submissions, all of them either "
              f"carry answers or are outstanding on an active form.")
        raise SystemExit

    print(f"{len(doomed)} of {total} submissions would be removed:\n")
    for sub, why in doomed:
        print(f"  {sub.id:3}  {sub.client.name:16} {sub.form.name[:34]:36} "
              f"{sub.status.value:9} {why}")

    keeping = total - len(doomed)
    print(f"\n{keeping} would stay, including every submission with answers.")

    if "--apply" not in sys.argv:
        print("\nThis was a preview. Re-run with --apply to remove them.")
        raise SystemExit

    n = apply(doomed)
    db = SessionLocal()
    print(f"\nremoved {n}; {db.query(Submission).count()} submissions remain")
    for s in db.query(Submission).order_by(Submission.id).all():
        print(f"  {s.id:3} {s.client.name:16} {s.form.name[:34]:36} "
              f"{s.status.value:9} answers={len(s.answers)}")
