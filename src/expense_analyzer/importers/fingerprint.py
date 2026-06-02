"""Import idempotency hash (design §5/§6).

The fingerprint is what makes import an upsert: importing the same file twice,
or overlapping daily exports, never creates duplicates. Computed exactly as the
design specifies — ``sha256(account_id + booked_date + amount + raw_description)``.

Known limitation (accepted by design): two genuinely distinct transactions with
the same account, date, amount and description collapse to one fingerprint, so
the second is treated as a duplicate. Revisit only if it bites in practice.
"""

import hashlib
from datetime import date


def compute_fingerprint(
    account_id: int,
    booked_date: date,
    amount: int,
    raw_description: str,
) -> str:
    payload = f"{account_id}|{booked_date.isoformat()}|{amount}|{raw_description}"

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
