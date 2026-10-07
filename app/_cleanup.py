from app.tender_store import get_connection

ids_to_clear = ["1a0f0c40063da6b6", "1a0f0c36c437f708"]

with get_connection() as conn:
    conn.execute(
        f"DELETE FROM email_analysis WHERE email_id IN ({','.join('?' for _ in ids_to_clear)})",
        ids_to_clear,
    )

print(f"Cleared {len(ids_to_clear)} rows.")