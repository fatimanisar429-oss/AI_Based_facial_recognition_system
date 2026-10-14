"""Command-line tools. The first administrator is created here, never through a web page,
so nobody can register themselves as admin."""
import re

import click
from flask import current_app
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from .blueprints.auth import find_account
from .db import get_db
from .timeutil import utc_now_text

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


@click.command("create-admin")
@click.option("--name", prompt="Full name")
@click.option("--email", prompt="Email")
@click.option("--phone", prompt="Phone number")
@click.password_option("--password", prompt="Password", confirmation_prompt=True)
@with_appcontext
def create_admin_command(name, email, phone, password):
    """Create an administrator account:  flask --app run create-admin"""
    email = email.strip().lower()
    name, phone = name.strip(), phone.strip()
    if not name or not phone:
        raise click.ClickException("Name and phone are required.")
    if not EMAIL_PATTERN.match(email):
        raise click.ClickException("That email address is not valid.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise click.ClickException(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if find_account(email) is not None:
        raise click.ClickException("An account with that email already exists.")

    db = get_db()
    db.execute(
        "INSERT INTO admin (full_name, email, phone, password_hash) VALUES (?, ?, ?, ?)",
        (name, email, phone, generate_password_hash(password)),
    )
    db.commit()
    click.echo(f"Administrator {email} created.")


@click.command("generate-biometric-key")
def generate_biometric_key_command():
    """Print a new key for BIOMETRIC_KEY:  flask --app run generate-biometric-key"""
    from .services.biometrics import new_key
    click.echo(new_key())
    click.echo("Put it in .env as BIOMETRIC_KEY=<key> and keep a safe backup of it.", err=True)


@click.command("create-demo-users")
@with_appcontext
def create_demo_users_command():
    """Create demo accounts for local testing:  flask --app run create-demo-users

    Safe to run again (existing accounts are kept). The demo warden and student get random passwords nobody knows:
    sign in to them with the quick-login buttons (DEV_QUICK_LOGIN=1), or reset the password as admin."""
    import secrets
    accounts = current_app.config["DEV_ACCOUNTS"]
    db = get_db()
    made = []

    def fresh_hash():
        return generate_password_hash(secrets.token_urlsafe(24))

    if find_account(accounts["admin"]) is None:
        db.execute("INSERT INTO admin (full_name, email, phone, password_hash) VALUES (?, ?, ?, ?)",
                   ("Demo Admin", accounts["admin"], "0300-0000000", fresh_hash()))
        made.append("admin")
    if find_account(accounts["warden"]) is None:
        db.execute("INSERT INTO warden (full_name, email, phone, gender, password_hash) VALUES (?, ?, ?, ?, ?)",
                   ("Demo Warden", accounts["warden"], "0300-1111111", "Female", fresh_hash()))
        made.append("warden")
    if find_account(accounts["student"]) is None:
        warden = db.execute("SELECT warden_id FROM warden WHERE email = ?", (accounts["warden"],)).fetchone()
        db.execute(
            "INSERT INTO student (registration_no, full_name, email, phone, room_no, department, session, gender, "
            "parent_name, parent_email, password_hash, warden_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("DEMO-001", "Demo Student", accounts["student"], "0300-2222222", "A-01", "Software Engineering", "2022-2026",
             "Female", "Demo Parent", "parent@hostel.local", fresh_hash(), warden["warden_id"] if warden else None, utc_now_text()))
        made.append("student")
    db.commit()
    click.echo("Created: " + (", ".join(made) if made else "nothing (all demo accounts already exist)"))
    click.echo("Turn on the login-page buttons with DEV_QUICK_LOGIN=1 in .env (local testing only).")


@click.command("send-weekly-reports")
@with_appcontext
def send_weekly_reports_command():
    """Create last week's reports and email the parents now:  flask --app run send-weekly-reports"""
    from .services import scheduler
    result = scheduler.run_weekly_reports(current_app._get_current_object())
    click.echo(f"Reports for {result['period'][0]} to {result['period'][1]}: {result['created']} created, {result['existing']} already existed.")


@click.command("send-test-email")
@click.option("--to", required=True, help="Address that receives the test message")
@with_appcontext
def send_test_email_command(to):
    """Check the mail settings:  flask --app run send-test-email --to you@example.com"""
    from .services import emailer
    ok, message = emailer.send_test(current_app._get_current_object(), to)
    click.echo(message)
    if not ok:
        raise click.ClickException("Email is not working yet.")


def init_app(app):
    app.cli.add_command(create_admin_command)
    app.cli.add_command(generate_biometric_key_command)
    app.cli.add_command(create_demo_users_command)
    app.cli.add_command(send_weekly_reports_command)
    app.cli.add_command(send_test_email_command)
