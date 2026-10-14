"""Database helpers: one SQLite connection per request, schema creation."""
import sqlite3
from pathlib import Path

import click
from flask import current_app, g
from flask.cli import with_appcontext

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def get_db():
    """Return this request's database connection (opened on first use)."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")  # SQLite ignores foreign keys unless asked
    return g.db


def connect(app):
    """A separate connection for background threads and jobs, which have no request. The caller must close it."""
    db = sqlite3.connect(app.config["DATABASE_PATH"], timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does not touch tables that already exist,
# so databases created by an older version get these columns added here (data is kept).
LATER_COLUMNS = (
    ("entry_log", "failure_code", "TEXT"),
    ("entry_log", "liveness", "TEXT"),
    ("student", "created_at", "TEXT"),
)


def _add_missing_columns(db):
    for table, column, ddl in LATER_COLUMNS:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    """Create all tables if missing and bring old databases up to date. Safe to run repeatedly; never deletes data."""
    db = get_db()
    db.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _add_missing_columns(db)
    db.commit()


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Flask CLI: flask --app run init-db"""
    init_db()
    click.echo("Database ready.")


def init_app(app):
    Path(app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    with app.app_context():
        init_db()  # also runs on startup so a fresh clone just works
