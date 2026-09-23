"""Patch the editor: Send to Client in the toolbar, selectable questions,
and the block-editor routes. Run once, then delete."""
from pathlib import Path

# ---------------------------------------------------------------- template
tpl = Path("app/templates/form_editor.html")
s = tpl.read_text(encoding="utf-8")

s = s.replace('  <a href="/forms" title="Back to all forms">&larr;</a>\n'
              '  <a href="/preview/{{ form.id }}">&#128269; Preview</a>',
              '  <a href="/forms" title="Back to all forms">&larr;</a>\n'
              '  <a href="/send">&#9993; Send to Client</a>\n'
              '  <a href="/preview/{{ form.id }}">&#128269; Preview</a>')

# Each question row becomes a link that selects it.
s = s.replace('''    <div class="ed-q">
      <input type="checkbox">
      <span class="n">{{ loop.index }} .</span>
      <div class="t">
        {{ q.text }}{% if q.required %} <span class="req">*</span>{% endif %}
        <div class="meta">
          {{ q.qtype.value }} &middot; page {{ q.page }}
          {%- if q.rows %} &middot; {{ q.rows|length }} rows{% endif %}
          {%- if q.options %} &middot; {{ q.options|length }} options{% endif %}
        </div>
      </div>''',
'''    <div class="ed-q {{ 'on' if selected and selected.id == q.id }}">
      <input type="checkbox">
      <a class="pick" href="/forms/{{ form.id }}?q={{ q.id }}">
        <span class="n">{{ loop.index }} .</span>
        <div class="t">
          {{ q.text }}{% if q.required %} <span class="req">*</span>{% endif %}
          <div class="meta">
            {{ TYPE_LABELS.get(q.qtype.value, q.qtype.value) }} &middot; page {{ q.page }}
            {%- if q.items %} &middot; {{ q.items|length }} items{% endif %}
            {%- if q.rows %} &middot; {{ q.rows|length }} rows{% endif %}
            {%- if q.options %} &middot; {{ q.options|length }} options{% endif %}
          </div>
        </div>
      </a>''')

# Right pane: the block editor when something is selected, else the help panel.
old_info = s[s.index('  <!-- ---------- right: what you can do ---------- -->'):s.index('</div>\n{% endblock %}')]
new_info = '''  <!-- ---------- right: block editor, or the help panel ---------- -->
  {% if selected %}
  {% include "_question_pane.html" %}
  {% else %}
''' + old_info.split('\n', 1)[1] + '''  {% endif %}
'''
s = s.replace(old_info, new_info)
tpl.write_text(s, encoding="utf-8")

# ---------------------------------------------------------------- routes
main = Path("app/main.py")
m = main.read_text(encoding="utf-8")

m = m.replace("    QuestionType, SessionLocal, Submission, SubmissionStatus, User, UserRole, log,",
              "    ItemKind, QuestionItem, QuestionType, SessionLocal, Submission,\n"
              "    SubmissionStatus, User, UserRole, log,")

m = m.replace('templates.env.globals["MIN_PW"] = auth.MIN_PASSWORD_LENGTH',
              'templates.env.globals["MIN_PW"] = auth.MIN_PASSWORD_LENGTH\n'
              'templates.env.globals["TYPE_LABELS"] = {\n'
              '    "mixed_controls": "Mixed Controls",\n'
              '    "radio": "Multiple Choice - Single Answer",\n'
              '    "checkbox": "Multiple Choice - Multiple Answers",\n'
              '    "long_text": "Open Answer",\n'
              '    "short_text": "Short Answer",\n'
              '    "matrix": "Matrix / Scale",\n'
              '    "file_upload": "File Upload",\n'
              '    "signature": "Signature",\n'
              '    "attestation": "Attestation",\n'
              '    "date": "Date",\n'
              '}')

m = m.replace('''def form_editor(form_id: int, request: Request, db: Session = Depends(get_db),
                _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    attached = {fc.consent_id for fc in form.consents}
    consents = db.query(ConsentForm).order_by(ConsentForm.name).all()
    return render(
        "form_editor.html",
        ctx(request, db, form=form, consents=consents, attached=attached,
            qtypes=list(QuestionType), nav="forms"),
    )''',
'''def form_editor(form_id: int, request: Request, q: int | None = None,
                db: Session = Depends(get_db),
                _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    attached = {fc.consent_id for fc in form.consents}
    consents = db.query(ConsentForm).order_by(ConsentForm.name).all()
    selected = db.get(Question, q) if q else None
    if selected and selected.form_id != form.id:
        selected = None            # never edit one form's question from another
    return render(
        "form_editor.html",
        ctx(request, db, form=form, consents=consents, attached=attached,
            qtypes=list(QuestionType), selected=selected, nav="forms"),
    )


@app.post("/questions/{qid}/text")
def question_text(qid: int, text: str = F(""), db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    if text.strip():
        q.text = text.strip()
        log(db, "Question Edited", "form", q.form_id)
        db.commit()
    return RedirectResponse(f"/forms/{q.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/type")
def question_type(qid: int, qtype: str = F("short_text"),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    try:
        q.qtype = QuestionType(qtype)
        log(db, "Question Type Changed", "form", q.form_id)
        db.commit()
    except ValueError:
        pass
    return RedirectResponse(f"/forms/{q.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/options")
def question_options(qid: int, options: str = F(""),
                     db: Session = Depends(get_db),
                     _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    q.options_raw = options
    log(db, "Question Options Changed", "form", q.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{q.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/matrix")
def question_matrix(qid: int, rows: str = F(""), options: str = F(""),
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    q.rows_raw, q.options_raw = rows, options
    log(db, "Matrix Changed", "form", q.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{q.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/items/new")
def item_new(qid: int, label: str = F(""), kind: str = F("text"),
             options: str = F(""), db: Session = Depends(get_db),
             _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    if label.strip():
        db.add(QuestionItem(question_id=q.id, label=label.strip(),
                            kind=ItemKind(kind), options_raw=options,
                            position=len(q.items)))
        log(db, "Item Added", "form", q.form_id)
        db.commit()
    return RedirectResponse(f"/forms/{q.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/duplicate")
def question_duplicate(qid: int, db: Session = Depends(get_db),
                       _=Depends(needs(perms.FORMS_EDIT))):
    src = get_or_404(db, Question, qid)
    copy = Question(form_id=src.form_id, text=f"{src.text} (copy)",
                    help_text=src.help_text, qtype=src.qtype, required=src.required,
                    position=src.position + 1, page=src.page,
                    options_raw=src.options_raw, rows_raw=src.rows_raw)
    db.add(copy)
    db.flush()
    for it in src.items:
        db.add(QuestionItem(question_id=copy.id, label=it.label, kind=it.kind,
                            options_raw=it.options_raw, position=it.position,
                            width=it.width))
    log(db, "Question Duplicated", "form", src.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{src.form_id}?q={copy.id}", status_code=303)''')

main.write_text(m, encoding="utf-8")
print("editor patched")
