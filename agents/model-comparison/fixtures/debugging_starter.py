def reconcile(rows):
    totals = {}
    seen = set()
    for row in rows:
        if row["account"] in seen:
            continue
        seen.add(row["account"])
        totals[row["account"]] = abs(row["amount_cents"])
    return totals
