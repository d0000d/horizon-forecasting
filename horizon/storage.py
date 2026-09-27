from contextlib import contextmanager
import sqlite3


@contextmanager
def connect(path):
    db = sqlite3.connect(path,timeout=30)
    try:
        with db:
            yield db
    finally:
        db.close()
