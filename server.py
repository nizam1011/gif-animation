"""Shape to Equations server: accounts, email verification, conversion allowance and admin approvals."""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import time
from collections import defaultdict, deque
from datetime import timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).parent
WEB = BASE / "web"
DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

DEFAULT_QUOTA = int(os.environ.get("DEFAULT_QUOTA", "10"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
MAIL_FROM = os.environ.get("MAIL_FROM") or SMTP_USER
APP_URL = os.environ.get("APP_URL", "").rstrip("/")

CODE_TTL = 15 * 60       # seconds a mailed code stays valid
CODE_ATTEMPTS = 5        # wrong guesses allowed per code
RESEND_WAIT = 60         # seconds between codes for the same account
APP_NAME = "Shape to Equations"


def load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = DATA / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


app = Flask(__name__, static_folder=None)
app.secret_key = load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# ---------- database ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    quota INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, purpose)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    amount INTEGER,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def connect():
    con = sqlite3.connect(DATA / "app.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


with connect() as _con:
    _con.execute("PRAGMA journal_mode = WAL")
    _con.executescript(SCHEMA)

# ---------- helpers ----------

now = lambda: int(time.time())
_hits = defaultdict(deque)   # in-memory rate limiting; the server runs as a single process


def recent(key, window):
    q, cutoff = _hits[key], time.time() - window
    while q and q[0] <= cutoff:
        q.popleft()
    return len(q)


def hit(key):
    _hits[key].append(time.time())


def ok(**extra):
    return jsonify(ok=True, **extra)


def err(message, status=400, **extra):
    return jsonify(ok=False, error=message, **extra), status


def norm_email(value):
    email = (value or "").strip().lower()
    return email if len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else None


def by_email(email):
    return db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def pw_tag(user):
    """Changes whenever the password does, so a reset signs out every other session."""
    return hashlib.sha256(user["password_hash"].encode()).hexdigest()[:16]


def sign_in(user):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["pv"] = pw_tag(user)


def current_user():
    if "user" not in g:
        uid = session.get("uid")
        row = db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
        if row and (not row["verified"] or session.get("pv") != pw_tag(row)):
            row = None
        g.user = row
    return g.user


def is_admin(user):
    return user["email"] in ADMIN_EMAILS


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return err("Please sign in.", 401)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return err("Please sign in.", 401)
        if not is_admin(user):
            return err("Admins only.", 403)
        return view(*args, **kwargs)
    return wrapped


def account(user):
    pending = db().execute("SELECT 1 FROM requests WHERE user_id = ? AND status = 'pending'", (user["id"],)).fetchone()
    admin = is_admin(user)
    return dict(email=user["email"], used=user["used"], quota=user["quota"], remaining=max(0, user["quota"] - user["used"]),
                unlimited=admin, is_admin=admin, pending=bool(pending))


# ---------- mail and codes ----------

def mail_config():
    """Mail account saved from the admin page, falling back to the environment."""
    saved = {r["key"]: r["value"] for r in db().execute("SELECT key, value FROM settings WHERE key LIKE 'smtp_%'")}
    if saved.get("smtp_user"):
        return dict(user=saved["smtp_user"], password=saved.get("smtp_password", ""), host=saved.get("smtp_host") or "smtp.gmail.com",
                    port=int(saved.get("smtp_port") or 587), sender=saved["smtp_user"], source="admin page")
    return dict(user=SMTP_USER, password=SMTP_PASSWORD, host=SMTP_HOST, port=SMTP_PORT, sender=MAIL_FROM,
                source="server environment" if SMTP_USER else "none")


def send_mail(to, subject, text, raise_errors=False):
    cfg = mail_config()
    if not cfg["user"]:
        # No mail account configured: show the message in the server log so the flow can still be used.
        print(f"[mail not sent: no mail account is set up] to={to} subject={subject!r}\n{text}", flush=True)
        return True
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"{APP_NAME} <{cfg['sender']}>", to, subject
    msg.set_content(text)
    try:
        if cfg["port"] == 465:
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
            smtp.starttls()
        with smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True
    except Exception:
        if raise_errors:
            raise
        app.logger.exception("Sending mail to %s failed", to)
        return False


def code_hash(user_id, purpose, code):
    return hmac.new(app.secret_key.encode(), f"{user_id}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def seconds_until_resend(user, purpose):
    row = db().execute("SELECT created_at FROM codes WHERE user_id = ? AND purpose = ?", (user["id"], purpose)).fetchone()
    return max(0, RESEND_WAIT - (now() - row["created_at"])) if row else 0


def mail_code(user, purpose):
    """Creates a fresh code and mails it. Returns an error response, or None on success."""
    ip = request.remote_addr
    if recent(("send", ip), 3600) >= 12:
        return err("Too many codes requested from this network. Try again in an hour.", 429)
    wait = seconds_until_resend(user, purpose)
    if wait:
        return err(f"A code was just sent. You can request another in {wait} seconds.", 429)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    db().execute("INSERT OR REPLACE INTO codes (user_id, purpose, code_hash, attempts, created_at) VALUES (?, ?, ?, 0, ?)",
                 (user["id"], purpose, code_hash(user["id"], purpose, code), now()))
    db().commit()
    hit(("send", ip))
    what = "confirm your email address" if purpose == "verify" else "reset your password"
    sent = send_mail(user["email"], f"{code} is your {APP_NAME} code",
                     f"Use this code to {what}:\n\n    {code}\n\nIt expires in {CODE_TTL // 60} minutes. "
                     "If you did not ask for it, you can ignore this email.\n")
    return None if sent else err("The email could not be sent. Please try again shortly.", 502)


def check_code(user, purpose, code):
    """Returns an error message, or None when the code is right (the code is then used up)."""
    con = db()
    row = con.execute("SELECT * FROM codes WHERE user_id = ? AND purpose = ?", (user["id"], purpose)).fetchone()
    if not row or now() - row["created_at"] > CODE_TTL:
        return "That code has expired. Request a new one."
    if row["attempts"] >= CODE_ATTEMPTS:
        return "Too many wrong attempts. Request a new code."
    if not hmac.compare_digest(row["code_hash"], code_hash(user["id"], purpose, str(code or "").strip())):
        con.execute("UPDATE codes SET attempts = attempts + 1 WHERE user_id = ? AND purpose = ?", (user["id"], purpose))
        con.commit()
        return "That code is not correct."
    con.execute("DELETE FROM codes WHERE user_id = ? AND purpose = ?", (user["id"], purpose))
    con.commit()
    return None


# ---------- request plumbing ----------

@app.before_request
def json_posts_only():
    # Requiring a JSON body means another site cannot trigger these with a plain form post.
    if request.method == "POST" and not request.is_json:
        return err("Expected a JSON request.", 415)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def page(name):
    resp = send_from_directory(WEB, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------- pages ----------

@app.get("/")
def index_page():
    return page("index.html") if current_user() else redirect("/login")


@app.get("/login")
def login_page():
    return redirect("/") if current_user() else page("login.html")


@app.get("/admin")
def admin_page():
    user = current_user()
    if not user:
        return redirect("/login")
    return page("admin.html") if is_admin(user) else redirect("/")


@app.get("/healthz")
def healthz():
    return "ok"


# ---------- sign-up, sign-in, password reset ----------

@app.post("/api/signup")
def signup():
    data = body()
    email, password = norm_email(data.get("email")), data.get("password") or ""
    if not email:
        return err("Enter a valid email address.")
    if not 8 <= len(password) <= 200:
        return err("Choose a password of at least 8 characters.")
    con, user = db(), by_email(email)
    if user and user["verified"]:
        return err("An account with that email already exists. Sign in instead.", 409)
    if user:
        if not seconds_until_resend(user, "verify"):
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
    else:
        con.execute("INSERT INTO users (email, password_hash, quota, created_at) VALUES (?, ?, ?, ?)",
                    (email, generate_password_hash(password), DEFAULT_QUOTA, now()))
    con.commit()
    return mail_code(by_email(email), "verify") or ok(email=email)


@app.post("/api/resend")
def resend():
    data = body()
    email, purpose = norm_email(data.get("email")), data.get("purpose")
    user = by_email(email) if email else None
    if purpose not in ("verify", "reset") or not user or (purpose == "verify" and user["verified"]):
        return ok()   # do not reveal whether the address has an account
    return mail_code(user, purpose) or ok()


@app.post("/api/verify")
def verify():
    data = body()
    email = norm_email(data.get("email"))
    user = by_email(email) if email else None
    if not user or user["verified"]:
        return err("That code has expired. Request a new one.")
    problem = check_code(user, "verify", data.get("code"))
    if problem:
        return err(problem)
    db().execute("UPDATE users SET verified = 1 WHERE id = ?", (user["id"],))
    db().commit()
    sign_in(user)
    return ok()


@app.post("/api/login")
def login():
    data = body()
    email, password = norm_email(data.get("email")), data.get("password") or ""
    key = ("login", request.remote_addr, email)
    if recent(key, 900) >= 10:
        return err("Too many failed attempts. Try again in 15 minutes.", 429)
    user = by_email(email) if email else None
    if not user or not check_password_hash(user["password_hash"], password):
        hit(key)
        return err("Wrong email or password.", 401)
    if not user["verified"]:
        problem = None if seconds_until_resend(user, "verify") else mail_code(user, "verify")
        return problem or err("Confirm your email first. We sent you a code.", 403, needs_verify=True, email=email)
    sign_in(user)
    return ok()


@app.post("/api/logout")
def logout():
    session.clear()
    return ok()


@app.post("/api/reset/start")
def reset_start():
    email = norm_email(body().get("email"))
    if not email:
        return err("Enter a valid email address.")
    user = by_email(email)
    if user and user["verified"]:
        problem = mail_code(user, "reset")
        if problem:
            return problem
    return ok(email=email)   # same answer whether or not the account exists


@app.post("/api/reset/finish")
def reset_finish():
    data = body()
    email, password = norm_email(data.get("email")), data.get("password") or ""
    if not 8 <= len(password) <= 200:
        return err("Choose a password of at least 8 characters.")
    user = by_email(email) if email else None
    if not user or not user["verified"]:
        return err("That code has expired. Request a new one.")
    problem = check_code(user, "reset", data.get("code"))
    if problem:
        return err(problem)
    db().execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
    db().commit()
    sign_in(by_email(email))
    return ok()


# ---------- allowance ----------

@app.get("/api/me")
@login_required
def me():
    return ok(**account(current_user()))


@app.post("/api/convert")
@login_required
def convert():
    user, data, con = current_user(), body(), db()
    kind = data.get("kind") if data.get("kind") in ("image", "svg", "drawing") else "image"
    if is_admin(user):
        con.execute("UPDATE users SET used = used + 1 WHERE id = ?", (user["id"],))
    elif con.execute("UPDATE users SET used = used + 1 WHERE id = ? AND used < quota", (user["id"],)).rowcount == 0:
        return err(f"You have used all {user['quota']} conversions.", 403, error_code="quota", **account(user))
    con.execute("INSERT INTO conversions (user_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], kind, str(data.get("label") or "")[:120], now()))
    con.commit()
    g.pop("user")
    return ok(**account(current_user()))


@app.post("/api/request-more")
@login_required
def request_more():
    user, con = current_user(), db()
    if is_admin(user) or user["used"] < user["quota"]:
        return err("You still have conversions left.")
    if con.execute("SELECT 1 FROM requests WHERE user_id = ? AND status = 'pending'", (user["id"],)).fetchone():
        return err("You already have a request waiting for an admin.", 409)
    note = str(body().get("note") or "").strip()[:500]
    con.execute("INSERT INTO requests (user_id, note, created_at) VALUES (?, ?, ?)", (user["id"], note, now()))
    con.commit()
    link = f"\n\nReview it at {APP_URL}/admin" if APP_URL else ""
    for admin in ADMIN_EMAILS:
        send_mail(admin, f"{APP_NAME}: {user['email']} asked for more conversions",
                  f"{user['email']} has used {user['used']} of {user['quota']} conversions and asked for more."
                  + (f"\n\nTheir note: {note}" if note else "") + link + "\n")
    return ok(**account(user))


# ---------- admin ----------

@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    con = db()
    reqs = con.execute("""SELECT r.id, r.note, r.status, r.amount, r.created_at, r.decided_at, r.decided_by, u.email, u.used, u.quota
                          FROM requests r JOIN users u ON u.id = r.user_id
                          ORDER BY r.status = 'pending' DESC, r.created_at DESC LIMIT 200""").fetchall()
    users = con.execute("""SELECT u.id, u.email, u.verified, u.used, u.quota, u.created_at,
                                  (SELECT MAX(created_at) FROM conversions c WHERE c.user_id = u.id) AS last_conversion
                           FROM users u ORDER BY u.created_at DESC""").fetchall()
    cfg = mail_config()
    return ok(requests=[dict(r) for r in reqs],
              users=[dict(u, is_admin=u["email"] in ADMIN_EMAILS) for u in users],
              default_grant=DEFAULT_QUOTA,
              # The password itself is never sent back to the browser.
              mail=dict(user=cfg["user"], host=cfg["host"], port=cfg["port"], has_password=bool(cfg["password"]), source=cfg["source"]))


@app.post("/api/admin/mail")
@admin_required
def admin_save_mail():
    data, con = body(), db()
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "").replace(" ", "")   # Google shows app passwords in spaced groups
    host = str(data.get("host") or "smtp.gmail.com").strip()
    try:
        port = int(data.get("port") or 587)
    except (TypeError, ValueError):
        port = 0
    if not norm_email(user):
        return err("Enter the full email address of the sending account.")
    if not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        return err("Enter a valid mail server and port.")
    values = dict(smtp_user=user, smtp_host=host, smtp_port=str(port))
    if password:
        values["smtp_password"] = password   # a blank field keeps the password already saved
    elif not con.execute("SELECT 1 FROM settings WHERE key = 'smtp_password'").fetchone():
        return err("Enter the app password.")
    con.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", values.items())
    con.commit()
    return ok()


@app.post("/api/admin/mail/test")
@admin_required
def admin_test_mail():
    if mail_config()["source"] == "none":
        return err("Save a mail account first.")
    to = current_user()["email"]
    try:
        send_mail(to, f"{APP_NAME}: test email", "This is a test. If you can read it, verification emails will be delivered.\n", raise_errors=True)
    except smtplib.SMTPAuthenticationError:
        return err("The mail server rejected the address or app password.", 502)
    except Exception as exc:
        return err(f"Could not send: {type(exc).__name__}: {exc}"[:300], 502)
    return ok(to=to)


@app.post("/api/admin/requests/<int:request_id>")
@admin_required
def admin_decide(request_id):
    data, con = body(), db()
    row = con.execute("SELECT r.*, u.email FROM requests r JOIN users u ON u.id = r.user_id WHERE r.id = ?", (request_id,)).fetchone()
    if not row or row["status"] != "pending":
        return err("That request has already been handled.", 409)
    stamp = (now(), current_user()["email"], request_id)
    if data.get("action") == "approve":
        try:
            amount = int(data.get("amount", DEFAULT_QUOTA))
        except (TypeError, ValueError):
            amount = 0
        if not 1 <= amount <= 10000:
            return err("Enter how many conversions to add (1 to 10000).")
        con.execute("UPDATE users SET quota = quota + ? WHERE id = ?", (amount, row["user_id"]))
        con.execute("UPDATE requests SET status = 'approved', amount = ?, decided_at = ?, decided_by = ? WHERE id = ?", (amount, *stamp))
        con.commit()
        send_mail(row["email"], f"{APP_NAME}: {amount} more conversions added",
                  f"Your request was approved and {amount} more conversions were added to your account."
                  + (f"\n\n{APP_URL}" if APP_URL else "") + "\n")
    elif data.get("action") == "deny":
        con.execute("UPDATE requests SET status = 'denied', decided_at = ?, decided_by = ? WHERE id = ?", stamp)
        con.commit()
        send_mail(row["email"], f"{APP_NAME}: request for more conversions",
                  "Your request for more conversions was not approved this time.\n")
    else:
        return err("Unknown action.")
    return ok()


@app.post("/api/admin/users/<int:user_id>")
@admin_required
def admin_set_quota(user_id):
    try:
        quota = int(body().get("quota"))
    except (TypeError, ValueError):
        return err("Enter a whole number.")
    if not 0 <= quota <= 1000000:
        return err("Enter a number between 0 and 1000000.")
    if db().execute("UPDATE users SET quota = ? WHERE id = ?", (quota, user_id)).rowcount == 0:
        return err("No such user.", 404)
    db().commit()
    return ok()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
