import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def new_id():
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS exams (
                    id TEXT PRIMARY KEY, started REAL NOT NULL,
                    policy TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL,
                    policy TEXT NOT NULL, exam_id TEXT, blocked TEXT
                );
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL, prompt_estimate INTEGER NOT NULL,
                    max_tokens INTEGER NOT NULL, usage TEXT, finish_reason TEXT,
                    upstream_id TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL, generation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, reasoning TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,seq);
                CREATE UNIQUE INDEX IF NOT EXISTS one_generation_running
                    ON generations(session_id) WHERE status = 'running';
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(generations)')}
            for column in ('backend', 'model'):
                if column not in columns:
                    db.execute(f'ALTER TABLE generations ADD COLUMN {column} TEXT')
            # A single-worker app recovers interrupted requests after restart.
            db.execute("UPDATE messages SET status='interrupted', active=0 WHERE generation_id IN (SELECT id FROM generations WHERE status='running')")
            db.execute("UPDATE generations SET status='interrupted', error='Ứng dụng đã khởi động lại.' WHERE status='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def one(self, query, args=()):
        with self.connect() as db:
            row = db.execute(query, args).fetchone()
            return dict(row) if row else None

    def all(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]

    def execute(self, query, args=()):
        with self.connect() as db:
            db.execute(query, args)

    def session(self, session_id):
        return self.one('SELECT * FROM sessions WHERE id=?', (session_id,))

    def messages(self, session_id):
        return self.all('SELECT * FROM messages WHERE session_id=? ORDER BY seq', (session_id,))

    def history(self, session_id):
        # Failed/cancelled drafts stay visible, but are not injected into context.
        return [{'role': m['role'], 'content': m['content']}
                for m in self.messages(session_id) if m['active'] and m['status'] == 'complete']

    def latest_exam(self):
        return self.one("SELECT * FROM exams WHERE state='active' ORDER BY started DESC LIMIT 1")

    def recover_reasoning_template_blocks(self, limit):
        """Repair only the known Cloudflare history-count bug, keeping audits.

        Missing usage, actual context violations and exam state are untouched.
        """
        reason = 'Sai lệch template vượt dự phòng. Phiên đã khóa để kiểm tra.'
        recovered = 0
        with self.connect() as db:
            rows = db.execute('''SELECT s.id, g.usage FROM sessions s JOIN generations g
                ON g.id=(SELECT id FROM generations WHERE session_id=s.id ORDER BY created DESC LIMIT 1)
                WHERE s.blocked=? AND g.backend='cloudflare' AND g.error=? AND g.status='complete'
                AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id
                    AND m.role='assistant' AND m.status='complete' AND m.content LIKE '%</think>%')''',
                              (reason, reason)).fetchall()
            for row in rows:
                try:
                    usage = json.loads(row['usage'])
                    prompt, completion = usage['prompt_tokens'], usage['completion_tokens']
                    valid = type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0
                except (TypeError, ValueError, KeyError):
                    continue
                if valid and prompt + completion <= limit:
                    db.execute('UPDATE sessions SET blocked=NULL WHERE id=?', (row['id'],))
                    recovered += 1
        return recovered

    def reserve(self, session, budget, text, backend=None, model=None):
        generation_id = new_id()
        with self.connect() as db:
            # Count policy is finalized only on success; errors do not evict history.
            db.execute('INSERT INTO generations(id,session_id,created,status,prompt_estimate,max_tokens,backend,model) VALUES(?,?,?,?,?,?,?,?)',
                       (generation_id, session['id'], time.time(), 'running', budget.prompt_tokens, budget.max_tokens, backend, model))
            db.execute('INSERT INTO messages(id,session_id,generation_id,role,content,status) VALUES(?,?,?,?,?,?)',
                       (new_id(), session['id'], generation_id, 'user', text, 'pending'))
            if session['title'] == 'Phiên mới':
                db.execute('UPDATE sessions SET title=? WHERE id=?', (text.replace('\n', ' ')[:55], session['id']))
        return generation_id

    def finish(self, session_id, generation_id, budget, content, reasoning, status,
               usage=None, finish_reason=None, upstream_id=None, error=None):
        with self.connect() as db:
            if status == 'complete' and budget.dropped_messages:
                rows = db.execute("SELECT seq FROM messages WHERE session_id=? AND active=1 AND status='complete' ORDER BY seq LIMIT ?",
                                  (session_id, budget.dropped_messages)).fetchall()
                for row in rows:
                    db.execute('UPDATE messages SET active=0 WHERE seq=?', (row['seq'],))
            db.execute('UPDATE messages SET status=? WHERE generation_id=?', (status, generation_id))
            db.execute('INSERT INTO messages(id,session_id,generation_id,role,content,reasoning,status,active) VALUES(?,?,?,?,?,?,?,?)',
                       (new_id(), session_id, generation_id, 'assistant', content, reasoning, status, int(status == 'complete')))
            db.execute('UPDATE generations SET status=?,usage=?,finish_reason=?,upstream_id=?,error=? WHERE id=?',
                       (status, json.dumps(usage) if usage else None, finish_reason, upstream_id, error, generation_id))
