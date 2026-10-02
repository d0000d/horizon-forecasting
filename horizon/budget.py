"""Durable, atomic EUR reservations. Unknown/failed call costs stay reserved."""
from .storage import connect
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4


class BudgetExceeded(RuntimeError):
    pass


def micros(euros):
    value = Decimal(str(euros))
    if not value.is_finite() or value < 0:
        raise ValueError("Invalid cost")
    return int((value*1_000_000).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, path, total_eur=100, run_eur=40, question_eur=20):
        self.path = str(path)
        self.limits = tuple(micros(x) for x in (total_eur, run_eur, question_eur))
        if not 0 < self.limits[2] <= self.limits[1] <= self.limits[0] <= micros(100):
            raise ValueError("Require 0 < question <= run <= total <= EUR 100")
        with connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, run TEXT, question TEXT, amount INTEGER, settled INTEGER)')

    def reserve(self, run, question, euros):
        amount = micros(euros)
        if amount == 0:
            raise ValueError("Reservation must be positive")
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            for clause, args, limit in (
                ('', (), self.limits[0]),
                (' WHERE run=?', (run,), self.limits[1]),
                (' WHERE run=? AND question=?', (run, question), self.limits[2]),
            ):
                used = db.execute('SELECT COALESCE(SUM(amount),0) FROM calls'+clause, args).fetchone()[0]
                if used+amount > limit:
                    raise BudgetExceeded("Budget reservation refused")
            ticket = str(uuid4())
            db.execute('INSERT INTO calls VALUES (?,?,?,?,0)', (ticket,run,question,amount))
            return ticket

    def settle(self, ticket, actual_eur):
        amount = micros(actual_eur)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT amount,settled FROM calls WHERE id=?',(ticket,)).fetchone()
            if row is None or row[1]:
                raise ValueError("Unknown/already settled reservation")
            # Record actual cost even if a provider violates its declared bound.
            db.execute('UPDATE calls SET amount=?,settled=1 WHERE id=?',(amount,ticket))
        if amount > row[0]:
            raise BudgetExceeded("Actual cost exceeded reservation; stop provider")

    def used_eur(self):
        with connect(self.path) as db:
            return db.execute('SELECT COALESCE(SUM(amount),0) FROM calls').fetchone()[0]/1_000_000
