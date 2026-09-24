"""Demo data for the billing, results and documents screens.

    python seed_billing_demo.py

Those screens were built against real query logic but no rows - so every one
of them renders as "nothing yet" until charges, claims, payments, orders and
documents actually exist. This gives them a few months of plausible activity
across the existing seeded patients, so Analytics has something to chart,
Insurance Collections has claims in more than one state, Patient Collections
has an aged balance, /results has something waiting for review, and the
Charges search has more than one status to filter between.

None of this is real financial or clinical data - amounts, payers and dates
are invented, the same way the seeded patients themselves are.

Safe to re-run: it does nothing if a charge already exists, the same
"skip if already there" rule seed.py itself follows.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta

from app.models import Client, SessionLocal, init_db
from app import documents, ehr

random.seed(2026)

PAYERS = ["Aetna", "Blue Shield of California", "Cigna", "Anthem BCBS",
          "Medicare", "Self-pay"]

CPT_AMOUNTS = {
    "90791": 250, "90792": 300, "99213": 150, "99214": 200, "99215": 260,
    "90832": 110, "90834": 175, "90837": 225, "90833": 90, "90836": 130,
}

LAB_NAMES = ["CBC with differential", "Comprehensive metabolic panel",
            "Lithium level", "TSH", "Lipid panel", "Valproic acid level"]


def _money(n: float) -> float:
    return round(n, 2)


def main() -> None:
    init_db()
    db = SessionLocal()

    if db.query(ehr.Charge).count() > 0:
        print("Charges already exist - skipping. Delete existing rows first "
              "if you want to reseed.")
        db.close()
        return

    clients = (db.query(Client).filter_by(archived=False)
               .order_by(Client.id).limit(10).all())
    if not clients:
        print("No clients to attach demo billing to - seed patients first.")
        db.close()
        return

    today = date.today()
    n_charges = n_claims = n_payments = n_orders = n_docs = n_encounters = 0

    for i, client in enumerate(clients):
        #  2-3 visits each, spread across the last four months.
        visits = random.randint(2, 3)
        for v in range(visits):
            seen_on = today - timedelta(days=random.randint(10, 120))
            cpt = random.choice(list(CPT_AMOUNTS))
            amount = CPT_AMOUNTS[cpt]

            enc = ehr.Encounter(
                client_id=client.id, seen_on=seen_on,
                reason="Medication review" if v else "Initial evaluation",
                template="Psychiatric Progress" if v else "Psychiatric Intake",
                status=ehr.NoteStatus.signed,
                signed_at=datetime.combine(seen_on, datetime.min.time()),
                signed_by="Dr Clinician")
            db.add(enc)
            db.flush()
            n_encounters += 1

            charge = ehr.Charge(
                client_id=client.id, encounter_id=enc.id, service_on=seen_on,
                cpt=cpt, description=dict(ehr.COMMON_CPT)[cpt],
                icd10="F41.1" if v else "F32.1", units=1, amount=amount,
                payer=random.choice(PAYERS))

            #  Distribute charges across the workflow so the status filter and
            #  the worklists all have something in more than one bucket.
            roll = (i + v) % 6
            if roll == 0:
                charge.status = ehr.ChargeStatus.draft
            elif roll == 1:
                charge.status = ehr.ChargeStatus.pending_approval
            elif roll == 2:
                charge.status = ehr.ChargeStatus.approved
            else:
                charge.status = ehr.ChargeStatus.submitted
                charge.submitted_at = datetime.combine(
                    seen_on + timedelta(days=1), datetime.min.time())
            db.add(charge)
            db.flush()
            n_charges += 1

            if charge.status != ehr.ChargeStatus.submitted:
                continue

            #  Every submitted charge gets a claim; roughly half resolve clean,
            #  the rest spread across the states a biller actually has to work.
            outcome = (i + v) % 5
            claim = ehr.Claim(
                charge_id=charge.id, client_id=client.id, payer=charge.payer,
                claim_number=f"CLM{1000 + charge.id}",
                billed=amount, submitted_on=charge.submitted_at.date(),
                created_by="Dr Clinician")

            if outcome == 0:
                claim.status = ehr.ClaimStatus.paid
                claim.responded_on = seen_on + timedelta(days=18)
                claim.allowed = _money(amount * 0.8)
                claim.paid = claim.allowed
                claim.adjustment = _money(amount - claim.allowed)
                charge.status = ehr.ChargeStatus.paid
                charge.paid_amount = claim.paid
                charge.paid_at = datetime.combine(claim.responded_on, datetime.min.time())
            elif outcome == 1:
                claim.status = ehr.ClaimStatus.denied
                claim.responded_on = seen_on + timedelta(days=15)
                claim.denial_code = "CO-50"
                claim.denial_reason = "Not medically necessary per payer review"
                charge.status = ehr.ChargeStatus.denied
            elif outcome == 2:
                claim.status = ehr.ClaimStatus.rejected
                claim.responded_on = seen_on + timedelta(days=4)
                claim.denial_code = "A7"
                claim.denial_reason = "Missing or invalid subscriber ID"
            elif outcome == 3:
                claim.status = ehr.ClaimStatus.needs_investigation
                claim.responded_on = seen_on + timedelta(days=10)
                claim.denial_reason = "Payer says no claim on file - call to trace"
            else:
                claim.status = ehr.ClaimStatus.waiting_adjudication

            db.add(claim)
            db.flush()
            n_claims += 1

            if claim.status == ehr.ClaimStatus.paid:
                db.add(ehr.Payment(
                    client_id=client.id, charge_id=charge.id, claim_id=claim.id,
                    amount=claim.paid, source=ehr.PaymentSource.payer,
                    method="EFT / ERA", reference=claim.claim_number,
                    received_on=claim.responded_on, posted_by="Front Desk"))
                n_payments += 1
                #  A third of paid claims leave a small patient co-pay owing,
                #  so Patient Collections has a real aged balance to show.
                if (i + v) % 3 == 0:
                    remainder = _money(amount - claim.paid)
                    if remainder > 0:
                        charge.amount = amount  # unchanged; remainder is owed
                        db.add(ehr.Payment(
                            client_id=client.id, charge_id=charge.id,
                            amount=_money(remainder * 0.4), source=ehr.PaymentSource.patient,
                            method="Card", reference="",
                            received_on=claim.responded_on + timedelta(days=20),
                            posted_by="Front Desk"))
                        n_payments += 1

        #  One lab order per patient - most resulted, so /results has a queue.
        order = ehr.Order(
            client_id=client.id, kind="lab", name=random.choice(LAB_NAMES),
            reason="Routine monitoring", ordered_on=today - timedelta(days=20))
        if i % 4 == 0:
            order.status = ehr.OrderStatus.placed
            order.placed_at = datetime.combine(order.ordered_on, datetime.min.time())
        else:
            order.status = ehr.OrderStatus.resulted
            order.placed_at = datetime.combine(order.ordered_on, datetime.min.time())
            order.result_text = "Within normal limits." if i % 3 else "Mildly elevated - recheck in 4 weeks."
            order.result_abnormal = bool(i % 3 == 0)
            order.result_at = datetime.combine(today - timedelta(days=2), datetime.min.time())
            #  Most left unreviewed - that queue is the point of the screen.
            if i % 5 == 0:
                order.reviewed_by = "Dr Clinician"
                order.reviewed_at = datetime.utcnow()
        db.add(order)
        n_orders += 1

    #  A handful of documents - some already filed, most still in the unfiled
    #  queue, since that queue-not-archive framing is what documents.html tests.
    sample_files = [
        ("Insurance card - front.txt", "text/plain",
         b"DEMO INSURANCE CARD - Aetna - Member ID X123456789 - Group 00214"),
        ("Referral letter.txt", "text/plain",
         b"DEMO REFERRAL - referring physician requests psychiatric evaluation."),
        ("Outside records - discharge summary.txt", "text/plain",
         b"DEMO DISCHARGE SUMMARY - admitted for evaluation, discharged stable."),
    ]
    for idx, (name, media, content) in enumerate(sample_files):
        client = clients[idx % len(clients)] if idx < 2 else None  # last one unfiled
        db.add(documents.Document(
            client_id=client.id if client else None,
            name=name, file_name=name, media_type=media, size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), content=content,
            label=["Insurance card", "Referral letter", "Outside records"][idx],
            received_on=today - timedelta(days=5 + idx),
            received_from="Demo fax line",
            processed=bool(client), processed_by="Front Desk" if client else "",
            processed_at=datetime.utcnow() if client else None,
            uploaded_by="Front Desk"))
        n_docs += 1

    db.commit()
    print(f"Seeded {n_encounters} encounters, {n_charges} charges, "
         f"{n_claims} claims, {n_payments} payments, {n_orders} orders, "
         f"{n_docs} documents across {len(clients)} patients.")
    db.close()


if __name__ == "__main__":
    main()
