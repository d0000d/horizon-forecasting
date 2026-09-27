import json
from .storage import connect
from dataclasses import asdict
from datetime import datetime
from uuid import uuid4
from .models import ForecastRecord


def encode(value):
    return json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else asdict(v),
                      allow_nan=False, ensure_ascii=False)


class Memory:
    def __init__(self, path):
        self.path = str(path)
        with connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS forecasts (id TEXT PRIMARY KEY, question TEXT, timestamp TEXT, record TEXT)')

    def save(self, record: ForecastRecord):
        with connect(self.path) as db:
            db.execute('INSERT INTO forecasts VALUES (?,?,?,?)',
                       (str(uuid4()),record.question.id,record.question.as_of.isoformat(),encode(record)))

    def records(self):
        with connect(self.path) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM forecasts ORDER BY rowid')]
