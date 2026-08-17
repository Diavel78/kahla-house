"""THE CELLAR — crash-safe intent journal.

Spec: docs/cellar-migration-spec.md §5 ("an intent journal").

THE PROBLEM THIS SOLVES. A re-peg is CANCEL -> verify -> CREATE (the venue's
own documented sequence; `orders.modify` is banned -- see CLAUDE.md). If the
process dies between the cancel and the create, the bet is silently unquoted
and nobody knows. On Vercel that state was unfixable: a killed lambda leaves
no trace and there is no "next boot" to clean up after it.

A long-lived process CAN fix it. Write the intent to local durable storage
BEFORE touching the venue, clear it after the paired write confirms. On boot,
anything still open is a wound: reconcile it against `orders.list` and either
finish it or shout.

SQLite, not Postgres, on purpose: this must survive the network being down,
which is one of the ways the process dies in the first place.

Durability: WAL + synchronous=FULL. Slower per write and worth it -- an
fsync we skipped is the order we lose.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

log = logging.getLogger("cellar.journal")

_SCHEMA = """
create table if not exists intents (
  id         integer primary key autoincrement,
  kind       text    not null,      -- 'repeg' | 'topup' | 'harvest' | ...
  key        text    not null,      -- slug / order id / pick id
  payload    text    not null,      -- json: everything needed to reconcile
  opened_at  real    not null,
  closed_at  real,
  outcome    text                   -- 'done' | 'aborted' | 'reconciled:<...>'
);
create index if not exists intents_open on intents (closed_at) where closed_at is null;
"""


class Journal:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._db.execute("pragma journal_mode=WAL")
        # FULL, not NORMAL: NORMAL can lose the last transaction on power loss,
        # and the last transaction is exactly the one describing the order we
        # were mid-way through placing.
        self._db.execute("pragma synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- lifecycle ----------------------------------------------------------

    def open(self, kind: str, key: str, **payload) -> int:
        cur = self._db.execute(
            "insert into intents (kind, key, payload, opened_at) values (?,?,?,?)",
            (kind, key, json.dumps(payload, default=str), time.time()),
        )
        self._db.commit()          # durable BEFORE the caller touches the venue
        return int(cur.lastrowid)

    def close(self, intent_id: int, outcome: str = "done") -> None:
        self._db.execute(
            "update intents set closed_at=?, outcome=? where id=?",
            (time.time(), outcome, intent_id),
        )
        self._db.commit()

    @contextmanager
    def intent(self, kind: str, key: str, **payload):
        """Scoped intent. An exception marks it aborted and RE-RAISES -- the
        row stays closed but its outcome records that we did not finish, so
        boot reconciliation can still see what happened."""
        iid = self.open(kind, key, **payload)
        try:
            yield iid
        except BaseException as e:
            self.close(iid, f"aborted:{type(e).__name__}")
            raise
        else:
            self.close(iid, "done")

    # -- recovery -----------------------------------------------------------

    def open_intents(self) -> list[dict]:
        """Everything that was in flight when we died. Called at boot."""
        rows = self._db.execute(
            "select id, kind, key, payload, opened_at from intents "
            "where closed_at is null order by opened_at"
        ).fetchall()
        out = []
        for i, kind, key, payload, opened in rows:
            try:
                p = json.loads(payload)
            except Exception:
                p = {"_unparseable": payload}
            out.append({"id": i, "kind": kind, "key": key,
                        "payload": p, "opened_at": opened,
                        "age_s": round(time.time() - opened, 1)})
        return out

    def prune(self, older_than_days: float = 14.0) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self._db.execute(
            "delete from intents where closed_at is not null and closed_at < ?",
            (cutoff,),
        )
        self._db.commit()
        return cur.rowcount

    def close_db(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass
