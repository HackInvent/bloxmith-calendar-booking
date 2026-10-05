"""Private bounded SQLite receipts; reserve creation durably before sending any event."""

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
from . import policy


class Journal:
    def __init__(self, storage, cfg):
        self.storage, self.cfg = Path(storage),cfg
        self.lock, self.db = None,None

    def __enter__(self):
        try:
            if self.storage.is_symlink() or not self.storage.is_dir():
                policy.fail("A private node storage directory is required.","storage")
            self.lock = os.open(self.storage/"calendar.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            info = os.fstat(self.lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                policy.fail("Invalid calendar journal lock.","storage")
            try:
                fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                policy.fail("This block is busy; send one calendar operation at a time.","busy")
            path = self.storage/"calendar.sqlite3"
            if not path.exists():
                fd = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600); os.close(fd)
            for suffix in ("","-journal","-wal","-shm"):
                item = Path(str(path)+suffix)
                if item.is_symlink():
                    policy.fail("Unexpected journal symbolic link.","storage")
                if item.exists():
                    info = item.stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>self.cfg["max_storage_bytes"]:
                        policy.fail("Invalid or oversized private journal.","storage")
            self.db = sqlite3.connect(str(path),timeout=1,isolation_level=None)
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            page_size=self.db.execute("PRAGMA page_size").fetchone()[0]
            limit=self.cfg["max_storage_bytes"]//page_size
            if self.db.execute("PRAGMA max_page_count="+str(limit)).fetchone()[0]>limit:
                policy.fail("Journal already exceeds its configured limit.","storage")
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0,1):
                policy.fail("Unsupported journal schema; no automatic reset.","storage")
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            self.db.execute("PRAGMA user_version=1")
            return self
        except Exception:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*args):
        if self.db:
            self.db.close(); self.db=None
        if self.lock is not None:
            os.close(self.lock); self.lock=None

    def get(self,ident):
        row = self.db.execute("SELECT payload FROM jobs WHERE id=?",(ident,)).fetchone()
        return policy.document(row[0],262144) if row else None

    def all(self):
        for row in self.db.execute("SELECT payload FROM jobs"):
            yield policy.document(row[0],262144)

    def save(self,record):
        if not self.get(record["request_id"]) and self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]>=self.cfg["max_records"]:
            policy.fail("Receipt limit reached; no idempotency history was removed.","storage")
        self.db.execute("INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (record["request_id"],policy.encoded(record,262144)))
