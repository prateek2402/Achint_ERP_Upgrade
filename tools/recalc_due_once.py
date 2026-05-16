import sqlite3


def main() -> None:
    db = sqlite3.connect("erp_database.sqlite", timeout=60)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    cur.execute("PRAGMA busy_timeout=60000")

    # 1) New due rule: receivable is gross less advance.
    cur.execute(
        """
        UPDATE invoices
        SET net_payable = MAX(0, COALESCE(total,0) - COALESCE(advance_adj,0))
        WHERE COALESCE(is_note,0)=0
        """
    )

    # 2) Preserve manual paid seed, clamped to payable.
    cur.execute(
        """
        UPDATE invoices
        SET paid = MIN(MAX(COALESCE(paid,0), 0), COALESCE(net_payable,0)),
            balance = MAX(0, COALESCE(net_payable,0) - MIN(MAX(COALESCE(paid,0), 0), COALESCE(net_payable,0)))
        WHERE COALESCE(is_note,0)=0
        """
    )

    # 3) Layer invoice allocations on top.
    cur.execute(
        """
        WITH alloc AS (
          SELECT pa.target_inv_id AS invoice_no, SUM(COALESCE(pa.amount,0)) AS amt
          FROM payment_allocations pa
          WHERE pa.alloc_type='invoice' AND pa.target_inv_id IS NOT NULL
          GROUP BY pa.target_inv_id
        )
        UPDATE invoices
        SET paid = MIN(
                COALESCE(net_payable,0),
                COALESCE(paid,0) + COALESCE((SELECT amt FROM alloc WHERE alloc.invoice_no=invoices.invoice_no),0)
            ),
            balance = MAX(
                0,
                COALESCE(net_payable,0) - MIN(
                    COALESCE(net_payable,0),
                    COALESCE(paid,0) + COALESCE((SELECT amt FROM alloc WHERE alloc.invoice_no=invoices.invoice_no),0)
                )
            )
        WHERE COALESCE(is_note,0)=0
        """
    )

    # 4) Recompute client excess funds.
    cur.execute("SELECT id FROM clients")
    client_ids = [r[0] for r in cur.fetchall()]
    for cid in client_ids:
        cur.execute(
            """
            SELECT ph.id, ph.type, COALESCE(ph.amount,0) AS amount
            FROM payment_history ph
            WHERE ph.client_id=?
            """,
            (cid,),
        )
        total_excess = 0.0
        for p in cur.fetchall():
            cur.execute(
                """
                SELECT COALESCE(SUM(COALESCE(pa.amount,0)),0)
                FROM payment_allocations pa
                WHERE pa.payment_id=?
                  AND pa.alloc_type IN ('invoice','po_advance','po_advance_applied','note_allocation')
                """,
                (p["id"],),
            )
            alloc_sum = float(cur.fetchone()[0] or 0.0)
            if p["type"] == "RECEIPT":
                unalloc = float(p["amount"] or 0.0) - alloc_sum
                if unalloc > 0:
                    total_excess += unalloc
            elif p["type"] == "UNALLOCATED_APPLIED":
                total_excess -= alloc_sum
        cur.execute("UPDATE clients SET excess_funds=? WHERE id=?", (max(0.0, total_excess), cid))

    db.commit()
    print("clients_recalculated", len(client_ids))

    # 5) Audit condition reported by user.
    cur.execute(
        """
        SELECT COUNT(*)
        FROM invoices
        WHERE COALESCE(is_note,0)=0
          AND (COALESCE(total,0) - COALESCE(advance_adj,0) - COALESCE(paid,0)) > 0.01
          AND COALESCE(balance,0) <= 0.01
        """
    )
    print("mismatch_count_after", cur.fetchone()[0])
    db.close()


if __name__ == "__main__":
    main()
