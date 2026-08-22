import sqlite3


def test_backup_db_produces_consistent_snapshot(test_db, tmp_path):
    # put a recognisable row in the live DB
    rec_id = test_db.upsert_recommendation(
        "Backup Test Book", "Author A", None, "Book", "Yes", "reason")
    dest = tmp_path / "snapshot.db"

    test_db.backup_db(str(dest))

    snap = sqlite3.connect(dest)
    try:
        title = snap.execute(
            "SELECT title FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()[0]
    finally:
        snap.close()
    assert title == "Backup Test Book"
