"""Optional checkpoints in the app's existing PostgreSQL connection, in a new table only.
No collector tables or credentials are changed. Local-only storage is explicitly reported.
"""
import os
import sqlite3

class Checkpoint:
    def __init__(self, store):
        self.store=store
        self.dsn=(os.getenv('ALPHA500_STATE_DATABASE_URL') or os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL') or '').strip()
        self.status='PENDING_PERSISTENT_STORAGE' if not self.dsn else 'PENDING_CHECKPOINT'
        self.error=None
    def connection(self):
        import psycopg
        return psycopg.connect(self.dsn,connect_timeout=3,autocommit=True)
    def table(self, conn):
        conn.execute('CREATE TABLE IF NOT EXISTS operations_projection_checkpoint (key TEXT PRIMARY KEY, snapshot BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
    def restore(self):
        if not self.dsn:return
        try:
            with self.store.connect() as local:
                populated=local.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
            if populated:return
            with self.connection() as pg:
                self.table(pg);row=pg.execute("SELECT snapshot FROM operations_projection_checkpoint WHERE key='operations-v1'").fetchone()
            if row:
                # Restore via SQLite backup into the already-created destination, not an unsafe file replacement.
                mem=sqlite3.connect(':memory:')
                try:
                    mem.deserialize(bytes(row[0]))
                    if mem.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('CHECKPOINT_INTEGRITY')
                    with self.store.connect() as local:mem.backup(local)
                finally:mem.close()
            self.status='POSTGRES_RESTORED' if row else 'POSTGRES_READY'
        except Exception as e:self.error=type(e).__name__;self.status='PENDING_CHECKPOINT_RESTORE'
    def save(self):
        if not self.dsn:return
        try:
            mem=sqlite3.connect(':memory:')
            try:
                with self.store.connect() as local:local.backup(mem)
                snapshot=mem.serialize()
            finally:mem.close()
            if len(snapshot)>32_000_000:raise ValueError('CHECKPOINT_SIZE_REQUIRES_MIGRATION')
            with self.connection() as pg:
                self.table(pg)
                pg.execute("INSERT INTO operations_projection_checkpoint(key,snapshot) VALUES('operations-v1',%s) ON CONFLICT(key) DO UPDATE SET snapshot=EXCLUDED.snapshot,updated_at=NOW()",(snapshot,))
            self.status='POSTGRES_CHECKPOINTED';self.error=None
        except Exception as e:self.error=type(e).__name__;self.status='PENDING_CHECKPOINT_SAVE'
    def info(self):return {'status':self.status,'error':self.error,'local_database':True,'persistent_storage_verified':self.status in ('POSTGRES_RESTORED','POSTGRES_READY','POSTGRES_CHECKPOINTED')}
