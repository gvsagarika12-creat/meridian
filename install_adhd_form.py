"""Install (or reinstall) the ADHD in Adults Questionnaire.

    python install_adhd_form.py

Separate from seed.py on purpose. seed.py only ever runs against an empty
database, and this database is not empty - it holds real submissions now. This
script adds one form to a live database and can be run again after editing
app/packets_adhd.py.

Reinstalling rewrites the questions of the existing form rather than creating a
second one with the same name. A form that has already been sent out keeps its
identity, so submissions stay attached to it; its version number goes up, which
is how the app records that the paper changed.
"""

from __future__ import annotations

from app.models import (Form, Question, QuestionItem, SessionLocal, Submission,
                        init_db)
from app.packets_adhd import ADHD_PACKET

FORM_NAME = "ADHD in Adults Questionnaire"
DESCRIPTION = ("Research screening questionnaire for the adult ADHD trial. "
               "Self-reported history is compared against the hospital archive "
               "on the Records screen.")
COLOUR = "#3d9ad1"


def install() -> tuple[Form, int, int]:
    init_db()
    db = SessionLocal()

    form = db.query(Form).filter_by(name=FORM_NAME).first()
    if form:
        used_by = db.query(Submission).filter_by(form_id=form.id).count()
        for q in list(form.questions):
            db.delete(q)                      # items cascade with the question
        db.flush()
        form.version += 1
        form.description = DESCRIPTION
        print(f"rewriting the existing form (version {form.version}); "
              f"{used_by} submission(s) stay attached to it")
    else:
        form = Form(name=FORM_NAME, description=DESCRIPTION, colour=COLOUR,
                    is_active=True)
        db.add(form)
        db.flush()
        print(f"creating form {form.id}")

    n_items = 0
    for pos, (text, qtype, required, page, opts, rows, items) in enumerate(ADHD_PACKET):
        q = Question(form_id=form.id, text=text, qtype=qtype, required=required,
                     position=pos, page=page, options_raw=opts, rows_raw=rows)
        db.add(q)
        db.flush()
        for ipos, spec in enumerate(items):
            label, kind, iopts, width = spec[:4]
            db.add(QuestionItem(question_id=q.id, label=label, kind=kind,
                                options_raw=iopts, position=ipos, width=width,
                                required=spec[4] if len(spec) > 4 else False))
            n_items += 1

    db.commit()
    return form, len(ADHD_PACKET), n_items


if __name__ == "__main__":
    form, n_questions, n_items = install()
    db = SessionLocal()
    form = db.get(Form, form.id)
    pages = sorted({q.page for q in form.questions})
    print(f"\n{form.name}")
    print(f"  form id   {form.id}   version {form.version}")
    print(f"  questions {n_questions} across {len(pages)} pages")
    print(f"  fields    {n_items} inside the table blocks")
    for q in form.questions:
        kind = q.qtype.value
        extra = f" ({len(q.items)} fields)" if q.items else ""
        print(f"    p{q.page:<2} {kind:15}{extra:14} {q.text[:52]}")
