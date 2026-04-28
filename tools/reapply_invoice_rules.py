from decimal import Decimal, ROUND_HALF_UP

from main import SessionLocal, recalculate_client_ledger
from models import Client, Invoice


def round_inr(value: float) -> float:
    return float(Decimal(str(float(value or 0.0))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def main() -> None:
    db = SessionLocal()
    try:
        invoices = db.query(Invoice).all()
        rounded_basic_count = 0
        rounded_total_count = 0
        touched_invoices = 0

        for inv in invoices:
            old_basic = float(inv.basic or 0.0)
            old_total = float(inv.total or 0.0)
            new_basic = round_inr(old_basic)
            new_total = round_inr(old_total)

            changed = False
            if new_basic != old_basic:
                inv.basic = new_basic
                rounded_basic_count += 1
                changed = True
            if new_total != old_total:
                inv.total = new_total
                rounded_total_count += 1
                changed = True

            if changed:
                touched_invoices += 1

        db.commit()

        client_ids = [c.id for c in db.query(Client.id).all()]
        for client_id in client_ids:
            recalculate_client_ledger(client_id, db)

        print("REAPPLY_INVOICE_RULES_RESULT")
        print("invoices_total=", len(invoices))
        print("invoices_touched=", touched_invoices)
        print("rounded_basic_count=", rounded_basic_count)
        print("rounded_total_count=", rounded_total_count)
        print("clients_recalculated=", len(client_ids))
    finally:
        db.close()


if __name__ == "__main__":
    main()
