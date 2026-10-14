"""Application factory: builds and configures the Flask app."""
from flask import Flask, g, jsonify, redirect, url_for

from . import cli, db, navigation, security
from .services import emailer, scheduler
from .blueprints import (alerts, attendance, auth, contacts, dashboards, emails, enrollment, exits, guests, logs, reports, settings, students,
                         terminal, wardens)
from .config import Config
from .timeutil import format_local

INSECURE_KEYS = {"", "change-me", "dev-only-insecure-key"}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    # Refuse to run with a guessable secret key (it signs the login cookie).
    if app.config["SECRET_KEY"] in INSECURE_KEYS and not (app.config["TESTING"] or app.config["DEBUG"]):
        raise RuntimeError(
            "SECRET_KEY is not set. Copy .env.example to .env and put a long random value in it."
        )

    # {{ value|localtime }} shows a stored UTC timestamp in the hostel's timezone.
    app.jinja_env.filters["localtime"] = lambda value, pattern="%d %b %Y, %I:%M %p": format_local(
        value, app.config["TIMEZONE"], pattern
    )

    db.init_app(app)
    emailer.mail.init_app(app)
    security.init_app(app)
    navigation.init_app(app)
    cli.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboards.bp)
    app.register_blueprint(students.bp)
    app.register_blueprint(wardens.bp)
    app.register_blueprint(enrollment.bp)
    app.register_blueprint(terminal.bp)
    app.register_blueprint(attendance.bp)
    app.register_blueprint(logs.bp)
    app.register_blueprint(exits.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(guests.bp)
    app.register_blueprint(alerts.bp)
    app.register_blueprint(contacts.bp)
    app.register_blueprint(emails.bp)
    app.register_blueprint(reports.bp)

    @app.get("/health")
    def health():
        """Simple check that the app and database are working."""
        db.get_db().execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.get("/")
    def home():
        if g.user:
            return redirect(security.dashboard_url(g.user["role"]))
        return redirect(url_for("auth.login"))

    scheduler.init_scheduler(app)          # weekly report + email queue (not in tests, once per process)
    return app
