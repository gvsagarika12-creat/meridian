# data/

The practice's own records, imported into the app but never edited by it.

`hospital data analysis.csv` is the hospital archive — one row per visit, keyed
by the hospital's patient number. It is the Tebra stand-in: the system that
already holds a patient's history before they ever fill in a form here.

Load it, or reload it after the hospital sends a newer copy:

```bash
python import_hospital_data.py
```

That reads every CSV in this folder. Re-importing the same filename **replaces**
its rows rather than adding to them, so running it twice is harmless.

Expected columns:

```
Patient_ID, Age, Gender, Condition, Procedure,
Cost, Length_of_Stay, Readmission, Outcome, Satisfaction
```

Only `Patient_ID` and `Condition` are required. A row without a patient number
is skipped — there is nothing to attach it to.

The contents are gitignored. If these files ever hold real patient data they are
PHI, and a repository is the wrong place for them.
