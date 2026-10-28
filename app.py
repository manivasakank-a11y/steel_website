import csv, io, os, re, shutil, logging, threading, hashlib, sqlite3, time, secrets
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session
import mysql.connector
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# folder for files that must survive restarts (secret.key, import_state.db). In Docker = /data volume
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Private settings come from ENVIRONMENT VARIABLES (nothing secret is written in this file) ----------
# PowerShell example (set them before starting the site):
#   $env:DB_PASSWORD   = "your-mysql-password"
#   $env:IMPORT_FOLDER = "C:\Users\LN-1001\Desktop\ARS Input\LIMS Data"
#   $env:SIGNUP_CODE   = "your-secret-code"
#   $env:ADMIN_USERS   = "manivasakan"            (several admins: "user1,user2")

# ---------- Login settings ----------
# "0" works on http:// AND https:// links. "1" = stricter, but then ONLY https:// links can log in
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
MAX_LOGIN_TRIES = 5        # wrong passwords allowed ...
LOGIN_LOCK_SECONDS = 300   # ... then that computer is locked for 5 minutes
# only these user IDs can open "Manage users" and approve new users
ADMIN_USERS = [u.strip() for u in os.environ.get("ADMIN_USERS", "manivasakan").split(",") if u.strip()]

# ---------- New-user sign-up (on the login page) ----------
SIGNUP_CODE = os.environ.get("SIGNUP_CODE", "")   # optional: if set, people must type it to sign up
MAX_PENDING_SIGNUPS = 30   # sign-ups pause when this many are still waiting for approval
SIGNUP_LIMIT_PER_HOUR = 5  # sign-up attempts allowed per computer per hour

# ---------- Home page display settings ----------
SHOW_LAST_DAYS = 7         # home page shows only the last 7 days of data (by the `Date` column)
SHOW_MAX_ROWS = 1000       # maximum rows shown on the home page
# True  = searching a Heat No looks through ALL data
# False = searching a Heat No is also limited to the last SHOW_LAST_DAYS days
SEARCH_ALL_DATA = True


def load_secret_key():
    """Creates a random secret.key file the first time. Keep this file private (never put it on GitHub)."""
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),   # login lasts 8 hours
)

# ---------- Database settings (Docker sets these through docker-compose.yml) ----------
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")      # must match your MySQL password exactly
DB_NAME = os.environ.get("DB_NAME", "csv_upload_db")
TABLE = "steel_chemical_analysis"

# ---------- Auto-import settings ----------
# Default = an "import" folder next to this file. Set IMPORT_FOLDER to use another folder.
# In Docker: /import (mapped to your Windows folder in docker-compose.yml)
IMPORT_FOLDER = os.environ.get("IMPORT_FOLDER", os.path.join(BASE_DIR, "import"))
PROCESSED_FOLDER = os.path.join(IMPORT_FOLDER, "processed")
FAILED_FOLDER = os.path.join(IMPORT_FOLDER, "failed")
IMPORT_INTERVAL_MINUTES = 30      # folder is checked every 30 minutes
HEAT_COL = "Heat No"             # a Heat No that is already in the table is never added again
MIN_FILE_AGE_SECONDS = 10        # skip files still being copied/written

# False = leave files where they are (the state file remembers what was already read)
# True  = also move each file to processed/ (or failed/) after import
MOVE_FILES_AFTER_IMPORT = False

# ---------- Incremental-import settings ----------
STATE_DB = os.path.join(DATA_DIR, "import_state.db")      # remembers what was already read
ERROR_LOG = os.path.join(DATA_DIR, "import_errors.txt")   # reason for every failed file
HEAD_BYTES = 4096                # first bytes of a file used to recognise "same file, grown"
WATERMARK_MARGIN_SECONDS = 120   # re-check files touched in the last 2 min before the last scan
STATE_KEEP_DAYS = 30             # forget files not seen for 30 days (keeps state file small)
INSERT_BATCH = 2000              # rows per INSERT
LOOKUP_BATCH = 500               # Heat Nos per duplicate check query

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("auto_import")

if not DB_PASSWORD:
    log.warning("DB_PASSWORD is not set. In PowerShell run:  $env:DB_PASSWORD = \"your-mysql-password\"")

import_lock = threading.Lock()     # manual upload and timer never write at the same time
folder_lock = threading.Lock()     # two folder imports never run at the same time


def get_db():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d.%m.%Y"]
TIME_FORMATS = ["%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"]

def parse_date(v):
    for f in DATE_FORMATS:
        try: return datetime.strptime(v, f).date()
        except ValueError: pass
    return None

def parse_time(v):
    for f in TIME_FORMATS:
        try: return datetime.strptime(v, f).time()
        except ValueError: pass
    return None

def parse_num(v):
    try: return float(v)
    except ValueError: return None


def _s(x):
    """MySQL sometimes hands text back as bytes; always work with str."""
    return x.decode("utf-8", "ignore") if isinstance(x, (bytes, bytearray)) else x


def canon(s):
    """'  heat   NO ' -> 'heat no'  (header / Heat No matching ignores case and extra spaces)"""
    return re.sub(r"\s+", " ", str(_s(s)).strip()).lower()


def heat_key(v):
    """Same key for 1234, 1234.0 and '1234'; text ignores case and extra spaces."""
    v = _s(v)
    if isinstance(v, (float, Decimal)) and v == int(v):
        v = int(v)
    return canon(v)


# ---------- Core import (used by manual upload AND auto folder import) ----------
def import_csv_text(text):
    """
    Inserts every CSV row EXCEPT rows whose Heat No already exists
    (in the table, or earlier in the same text).
    Only the Heat Nos of THIS text are looked up in MySQL (fast with an index on `Heat No`);
    the whole table is never loaded.
    Returns (inserted, skipped_duplicates). Raises ValueError with a clear message on a bad CSV.
    """
    with import_lock:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(f"SHOW COLUMNS FROM {TABLE}")
            info = {_s(r[0]): str(_s(r[1])).lower() for r in cursor.fetchall()
                    if _s(r[0]) not in ("id", "uploaded_at", "row_hash")}
            db_map = {canon(c): c for c in info}          # 'heat no' -> 'Heat No'

            heat_col = db_map.get(canon(HEAT_COL))
            if heat_col is None:
                raise ValueError(f"Table has no '{HEAT_COL}' column.")

            # detect delimiter: comma, semicolon or tab (Excel in some regions uses ;)
            first_line = text.lstrip("\r\n").split("\n", 1)[0]
            delim = max([",", ";", "\t"], key=first_line.count)

            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            if not reader.fieldnames:
                raise ValueError("CSV file is empty.")

            # CSV header -> table column (ignores upper/lower case and extra spaces)
            header_map = {}
            for h in reader.fieldnames:
                dbc = db_map.get(canon(h)) if h else None
                if dbc and dbc not in header_map.values():
                    header_map[h] = dbc
            if not header_map:
                raise ValueError("No CSV headers match the table columns. "
                                 f"CSV headers: {reader.fieldnames}")

            cols = list(header_map.values())
            if heat_col not in cols:
                raise ValueError(f"CSV has no '{HEAT_COL}' column. CSV headers: {reader.fieldnames}")
            heat_idx = cols.index(heat_col)

            # 1) read the rows, drop repeats inside this text
            items, seen, skipped = [], set(), 0     # items = [(heat_key or None, values)]
            for row in reader:
                vals = []
                for h, c in header_map.items():
                    v = (row.get(h) or "").strip()
                    if v == "":
                        vals.append(None)
                    elif info[c] == "date":
                        vals.append(parse_date(v))
                    elif info[c] == "time":
                        vals.append(parse_time(v))
                    elif info[c].startswith("decimal"):
                        vals.append(parse_num(v))
                    else:
                        vals.append(v)
                if not any(x is not None for x in vals):
                    continue

                heat = vals[heat_idx]
                if heat is None:
                    items.append((None, vals))
                    continue
                k = heat_key(heat)
                if k in seen:
                    skipped += 1
                    continue
                seen.add(k)
                items.append((k, vals))

            # 2) ask MySQL which of THESE Heat Nos already exist (small indexed lookups)
            raw_heats = [vals[heat_idx] for k, vals in items if k is not None]
            found = set()
            for i in range(0, len(raw_heats), LOOKUP_BATCH):
                chunk = raw_heats[i:i + LOOKUP_BATCH]
                cursor.execute(
                    "SELECT `{c}` FROM {t} WHERE `{c}` IN ({p})".format(
                        c=heat_col, t=TABLE, p=", ".join(["%s"] * len(chunk))),
                    chunk)
                found.update(heat_key(r[0]) for r in cursor.fetchall() if r[0] is not None)

            data = []
            for k, vals in items:
                if k is not None and k in found:
                    skipped += 1
                else:
                    data.append(vals)

            # 3) insert only the new rows
            if data:
                # ON DUPLICATE KEY UPDATE id = id -> if a UNIQUE key on `Heat No` exists and a
                # duplicate still slips through, MySQL skips only that row (real errors are not hidden)
                sql = "INSERT INTO {} ({}) VALUES ({}) ON DUPLICATE KEY UPDATE `id` = `id`".format(
                    TABLE,
                    ", ".join(f"`{c}`" for c in cols),
                    ", ".join(["%s"] * len(cols)))
                for i in range(0, len(data), INSERT_BATCH):
                    cursor.executemany(sql, data[i:i + INSERT_BATCH])
                conn.commit()
            return len(data), skipped
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()


# ---------- State file: what was already read ----------
def state_conn():
    db = sqlite3.connect(STATE_DB, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS files ("
               "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, done_offset INTEGER, "
               "head_len INTEGER, head_hash TEXT, updated REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return db


def meta_get(db, key, default=None):
    r = db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))


def head_info(path):
    """(length, sha1) of the first HEAD_BYTES bytes of the file."""
    with open(path, "rb") as f:
        b = f.read(HEAD_BYTES)
    return len(b), hashlib.sha1(b).hexdigest()


def head_matches(path, head_len, head_hash):
    """True if the file STARTS with exactly the bytes we saw before (i.e. same data, only grown)."""
    if not head_len or not head_hash:
        return False
    with open(path, "rb") as f:
        b = f.read(head_len)
    return len(b) == head_len and hashlib.sha1(b).hexdigest() == head_hash


def decode_bytes(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:                   # CSV saved from Excel in ANSI
        return raw.decode("cp1252")


# ---------- Auto folder import ----------
def _move(path, dest_folder):
    os.makedirs(dest_folder, exist_ok=True)
    name = os.path.basename(path)
    target = os.path.join(dest_folder, name)
    if os.path.exists(target):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(name)
        target = os.path.join(dest_folder, f"{base}_{stamp}{ext}")
    shutil.move(path, target)
    return target


def process_file(db, path, st, rec):
    """
    Read only the NEW part of one CSV file and import it.
      - same file that grew          -> read from where we stopped last time
      - new file that continues the previous one (1,2,3 -> 1,2,3,4) -> skip the part already read
      - anything else                -> read the whole file (duplicate Heat Nos are still skipped)
    rec = saved row (size, mtime_ns, done_offset, head_len, head_hash) or None.
    Returns (inserted, skipped, start_offset).
    """
    size = st.st_size
    start = 0
    if rec:
        if rec[2] > 0 and size >= rec[2] and head_matches(path, rec[3], rec[4]):
            start = rec[2]
    else:
        last = db.execute("SELECT done_offset, head_len, head_hash FROM files "
                          "WHERE done_offset > 0 ORDER BY updated DESC LIMIT 1").fetchone()
        if last and size >= last[0] and head_matches(path, last[1], last[2]):
            start = last[0]

    with open(path, "rb") as f:
        header = f.readline()
        if start > len(header):
            f.seek(start)
            tail = f.read()
            raw = header + tail
            end = start + len(tail)
            has_new = bool(tail.strip())
        else:
            start = 0
            f.seek(0)
            raw = f.read()
            end = len(raw)
            has_new = True

    inserted = skipped = 0
    if has_new:
        inserted, skipped = import_csv_text(decode_bytes(raw))

    hl, hh = head_info(path)
    db.execute("INSERT OR REPLACE INTO files "
               "(path, size, mtime_ns, done_offset, head_len, head_hash, updated) VALUES (?,?,?,?,?,?,?)",
               (path, size, st.st_mtime_ns, end, hl, hh, time.time()))
    db.commit()
    return inserted, skipped, start


def _record_failure(db, path, st, rec, err):
    with open(ERROR_LOG, "a", encoding="utf-8") as ef:
        ef.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{path}\t{type(err).__name__}: {err}\n")
    if rec:   # remember size/time so a broken file is not retried until it changes
        db.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE path = ?",
                   (st.st_size, st.st_mtime_ns, path))
    else:
        db.execute("INSERT OR REPLACE INTO files "
                   "(path, size, mtime_ns, done_offset, head_len, head_hash, updated) VALUES (?,?,?,?,?,?,?)",
                   (path, st.st_size, st.st_mtime_ns, 0, 0, "", time.time()))
    db.commit()


def _import_folder():
    if not os.path.isdir(IMPORT_FOLDER):
        os.makedirs(IMPORT_FOLDER, exist_ok=True)
        log.info("Created import folder: %s", IMPORT_FOLDER)
        return 0, 0, 0

    scan_start = time.time()
    db = state_conn()
    files_done = total_in = total_dup = 0
    aborted = False
    try:
        watermark = float(meta_get(db, "watermark", 0) or 0)
        floor = watermark - WATERMARK_MARGIN_SECONDS if watermark else 0

        with os.scandir(IMPORT_FOLDER) as it:
            for entry in it:
                if not entry.is_file() or not entry.name.lower().endswith(".csv"):
                    continue
                st = entry.stat()

                # old file, not touched since the last scan -> not even opened, no lookup
                newest = max(st.st_mtime, getattr(st, "st_birthtime", 0) or 0)
                if newest < floor:
                    continue
                if scan_start - st.st_mtime < MIN_FILE_AGE_SECONDS:
                    continue  # still being written; picked up next run

                path = entry.path
                rec = db.execute("SELECT size, mtime_ns, done_offset, head_len, head_hash "
                                 "FROM files WHERE path = ?", (path,)).fetchone()
                if rec and rec[0] == st.st_size and rec[1] == st.st_mtime_ns:
                    continue  # unchanged since last time

                try:
                    inserted, skipped, start = process_file(db, path, st, rec)
                    files_done += 1
                    total_in += inserted
                    total_dup += skipped
                    log.info("%s -> read from byte %d, inserted %d, duplicate Heat No skipped %d",
                             entry.name, start, inserted, skipped)
                    if MOVE_FILES_AFTER_IMPORT:
                        _move(path, PROCESSED_FOLDER)
                except (mysql.connector.InterfaceError, mysql.connector.OperationalError) as e:
                    # MySQL down / locked: stop now, nothing is marked as read, retried next time
                    log.error("MySQL problem, will retry next round: %s", e)
                    aborted = True
                    break
                except Exception as e:
                    log.exception("%s FAILED. Reason: %s", entry.name, e)
                    _record_failure(db, path, st, rec, e)
                    if MOVE_FILES_AFTER_IMPORT:
                        try:
                            dest = _move(path, FAILED_FOLDER)
                            with open(dest + ".error.txt", "w", encoding="utf-8") as ef:
                                ef.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{type(e).__name__}: {e}\n")
                        except Exception as me:
                            log.error("Could not move %s to failed folder: %s", entry.name, me)

        if not aborted:
            meta_set(db, "watermark", scan_start)      # next scan only looks at newer files
            db.execute("DELETE FROM files WHERE updated < ?",
                       (scan_start - STATE_KEEP_DAYS * 86400,))
        db.commit()
    finally:
        db.close()

    return files_done, total_in, total_dup


def import_folder():
    """Import only files that are new or changed since the last scan (never two at once)."""
    if not folder_lock.acquire(blocking=False):
        log.info("Previous import is still running - skipping this round.")
        return 0, 0, 0
    try:
        return _import_folder()
    finally:
        folder_lock.release()


_scheduler = None

def start_scheduler():
    """Safe to call more than once; the timer is only started the first time."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        import_folder,
        "interval",
        minutes=IMPORT_INTERVAL_MINUTES,
        next_run_time=datetime.now(),   # also run once right at startup
        max_instances=1,                # never run two imports at the same time
        coalesce=True,
        id="folder_import",
    )
    _scheduler.start()
    log.info("Auto-import started: %s every %d min", IMPORT_FOLDER, IMPORT_INTERVAL_MINUTES)


# ---------- Manual upload ----------
def handle_upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".csv"):
        flash("Please choose a .csv file.", "error")
        return redirect(url_for("upload"))

    try:
        inserted, skipped = import_csv_text(decode_bytes(file.stream.read()))
        flash(f"Uploaded {inserted} new rows. Skipped {skipped} rows (Heat No already exists).", "ok")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Upload failed: {e}", "error")
        return redirect(url_for("upload"))


# ---------- Safer pages ----------
@app.after_request
def add_safety_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.endpoint != "static":
        # after Log out, the Back button must not show old data
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- Login / logout ----------
_login_fails = {}      # ip -> (number of wrong tries, time of last wrong try)


def _client_ip():
    ip = request.remote_addr or "?"
    # opened through the tunnel: the real visitor address is in this header
    if os.environ.get("BEHIND_TUNNEL") == "1" or ip in ("127.0.0.1", "::1"):
        ip = request.headers.get("CF-Connecting-IP", ip)
    return ip


def _is_locked(ip):
    count, last = _login_fails.get(ip, (0, 0.0))
    if time.time() - last >= LOGIN_LOCK_SECONDS:
        _login_fails.pop(ip, None)
        return False
    return count >= MAX_LOGIN_TRIES


def _user_active(username):
    """False if the user was disabled (or deleted) by the administrator."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT active FROM app_users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        log.error("Could not check user status: %s", e)
        return True          # database hiccup: do not log everybody out


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user or not _user_active(user):
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------- Database: "pending" column (new sign-ups wait for approval) ----------
_schema_ok = False


def ensure_schema():
    """Adds the 'pending' column to app_users once. New sign-ups stay pending until an admin approves."""
    global _schema_ok
    if _schema_ok:
        return
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM app_users LIKE 'pending'")
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE app_users ADD COLUMN pending TINYINT NOT NULL DEFAULT 0")
            conn.commit()
        _schema_ok = True
    finally:
        cur.close()
        conn.close()


def render_login(tab="login", form_username=""):
    return render_template("login.html", tab=tab, form_username=form_username,
                           need_code=bool(SIGNUP_CODE))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("index"))

    if request.method == "POST":
        ip = _client_ip()
        if _is_locked(ip):
            flash("Too many wrong attempts. Please try again in 5 minutes.", "error")
            return render_login()

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            ensure_schema()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT password_hash, active, pending FROM app_users WHERE username = %s",
                        (username,))
            row = cur.fetchone()
            cur.close()
            conn.close()
        except Exception as e:
            log.error("Login database error: %s", e)
            flash("Login is not available right now. Please try again later.", "error")
            return render_login(form_username=username)

        if row and check_password_hash(_s(row[0]), password):
            if not row[1]:        # right password, but the account is not active
                if row[2]:
                    flash("Your account is waiting for administrator approval.", "error")
                else:
                    flash("This account is disabled. Please contact the administrator.", "error")
                return render_login(form_username=username)
            _login_fails.pop(ip, None)
            session.clear()
            session["user"] = username
            session.permanent = True
            return redirect(url_for("index"))

        count, _ = _login_fails.get(ip, (0, 0.0))
        _login_fails[ip] = (count + 1, time.time())
        flash("Wrong user ID or password.", "error")
        return render_login(form_username=username)

    return render_login()


_signups = {}      # ip -> times of recent sign-up attempts


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET" or session.get("user"):
        return redirect(url_for("login"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    code = request.form.get("code", "")

    def fail(message):
        flash(message, "error")
        return render_login(tab="register", form_username=username)

    done_message = "Account request sent. You can log in after the administrator approves your account."

    # hidden field that only robots fill in: pretend it worked, save nothing
    if request.form.get("website"):
        flash(done_message, "ok")
        return redirect(url_for("login"))

    ip = _client_ip()
    now = time.time()
    recent = [t for t in _signups.get(ip, []) if now - t < 3600]
    if len(recent) >= SIGNUP_LIMIT_PER_HOUR:
        _signups[ip] = recent
        return fail("Too many sign-up attempts from this computer. Please try again later.")
    _signups[ip] = recent + [now]

    if SIGNUP_CODE and not secrets.compare_digest(code.encode(), SIGNUP_CODE.encode()):
        return fail("Wrong sign-up code. Ask the administrator for the code.")
    if not USERNAME_RE.match(username):
        return fail("User ID must be 3-50 characters: letters, numbers and . _ - @ only.")
    if len(password) < 8:
        return fail("Password must have at least 8 characters.")
    if password != confirm:
        return fail("The two passwords do not match.")

    try:
        ensure_schema()
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM app_users WHERE pending = 1 AND active = 0")
            if cur.fetchone()[0] >= MAX_PENDING_SIGNUPS:
                return fail("New sign-ups are paused. Please contact the administrator.")
            cur.execute("SELECT 1 FROM app_users WHERE username = %s", (username,))
            if cur.fetchone():
                return fail(f"User ID '{username}' is already taken. Please choose another one.")
            cur.execute(
                "INSERT INTO app_users (username, password_hash, active, pending) VALUES (%s, %s, 0, 1)",
                (username, generate_password_hash(password)))
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        log.error("Sign-up error: %s", e)
        return fail("Sign-up is not available right now. Please try again later.")

    flash(done_message, "ok")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Admin: manage users ----------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,50}$")


def is_admin():
    me = (session.get("user") or "").lower()
    return me in [a.lower() for a in ADMIN_USERS]


@app.context_processor
def inject_is_admin():
    admin = is_admin()
    pending = 0
    if admin:                               # admins see how many sign-ups are waiting
        try:
            ensure_schema()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM app_users WHERE pending = 1 AND active = 0")
            pending = cur.fetchone()[0]
            cur.close()
            conn.close()
        except Exception:
            pending = 0
    return {"is_admin": admin, "pending_count": pending}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user or not _user_active(user):
            session.clear()
            return redirect(url_for("login"))
        if not is_admin():
            flash("Only an administrator can open that page.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    ensure_schema()
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", "").encode(), session.get("csrf", "").encode()):
            flash("The page expired. Please try again.", "error")
            return redirect(url_for("users"))

        action = request.form.get("action", "")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        try:
            conn = get_db()
            cur = conn.cursor()
            try:
                if action == "create":
                    if not USERNAME_RE.match(username):
                        flash("User ID must be 3-50 characters: letters, numbers and . _ - @ only.", "error")
                    elif len(password) < 8:
                        flash("Password must have at least 8 characters.", "error")
                    elif password != confirm:
                        flash("The two passwords do not match.", "error")
                    else:
                        cur.execute("SELECT 1 FROM app_users WHERE username = %s", (username,))
                        if cur.fetchone():
                            flash(f"User ID '{username}' already exists.", "error")
                        else:
                            # active=1, pending=0 written explicitly: admin-created users can log in at once
                            cur.execute(
                                "INSERT INTO app_users (username, password_hash, active, pending) "
                                "VALUES (%s, %s, 1, 0)",
                                (username, generate_password_hash(password)))
                            conn.commit()
                            flash(f"User '{username}' created. Give them the user ID and password.", "ok")

                elif action == "reset":
                    if len(password) < 8:
                        flash("New password must have at least 8 characters.", "error")
                    else:
                        cur.execute("UPDATE app_users SET password_hash = %s WHERE username = %s",
                                    (generate_password_hash(password), username))
                        conn.commit()
                        flash(f"Password for '{username}' was changed.", "ok")

                elif action in ("disable", "enable"):
                    if action == "disable" and username.lower() == (session.get("user") or "").lower():
                        flash("You cannot disable your own account.", "error")
                    else:
                        cur.execute("UPDATE app_users SET active = %s, pending = 0 WHERE username = %s",
                                    (1 if action == "enable" else 0, username))
                        conn.commit()
                        flash(f"User '{username}' was {'enabled' if action == 'enable' else 'disabled'}.", "ok")

                elif action == "approve":
                    cur.execute("UPDATE app_users SET active = 1, pending = 0 WHERE username = %s AND pending = 1",
                                (username,))
                    conn.commit()
                    flash(f"User '{username}' approved. They can log in now.", "ok")

                elif action == "reject":
                    cur.execute("DELETE FROM app_users WHERE username = %s AND pending = 1", (username,))
                    conn.commit()
                    flash(f"Sign-up request from '{username}' was rejected and removed.", "ok")
            finally:
                cur.close()
                conn.close()
        except Exception as e:
            log.error("User management error: %s", e)
            flash(f"Could not save: {e}", "error")
        return redirect(url_for("users"))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT username, active, pending, created_at FROM app_users ORDER BY pending DESC, username")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    for r in rows:
        r["username"] = _s(r["username"])
    return render_template("users.html", rows=rows, admins=[a.lower() for a in ADMIN_USERS])


# ---------- Home page ----------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        return handle_upload()

    heat_no = request.args.get("heat_no", "").strip()

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # only rows from the last SHOW_LAST_DAYS days (by the `Date` column)
    recent = "`Date` >= CURDATE() - INTERVAL %s DAY"

    if heat_no:
        if SEARCH_ALL_DATA:
            # searching a Heat No looks through ALL data
            cursor.execute(
                f"SELECT * FROM {TABLE} WHERE `Heat No` LIKE %s ORDER BY id DESC LIMIT %s",
                (f"%{heat_no}%", SHOW_MAX_ROWS)
            )
        else:
            cursor.execute(
                f"SELECT * FROM {TABLE} WHERE `Heat No` LIKE %s AND {recent} ORDER BY id DESC LIMIT %s",
                (f"%{heat_no}%", SHOW_LAST_DAYS, SHOW_MAX_ROWS)
            )
    else:
        cursor.execute(
            f"SELECT * FROM {TABLE} WHERE {recent} ORDER BY id DESC LIMIT %s",
            (SHOW_LAST_DAYS, SHOW_MAX_ROWS)
        )

    rows = cursor.fetchall()
    columns = [c for c in (rows[0].keys() if rows else []) if c != "row_hash"]

    cursor.close()
    conn.close()

    # page auto-refresh (every 3 minutes) is handled by JavaScript in index.html
    return render_template("index.html", rows=rows, columns=columns, heat_no=heat_no)

# ---------- Upload page ----------
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        return handle_upload()
    return render_template("upload.html")

# ---------- Run the folder import right now ----------
@app.route("/import-now")
@login_required
def import_now():
    files, inserted, dup = import_folder()
    flash(f"Folder import: {files} file(s), {inserted} new rows, {dup} duplicate Heat No skipped.", "ok")
    return redirect(url_for("index"))

# ---------- Run directly (python app.py) ----------
# python app.py is for testing on YOUR PC only (debug mode is unsafe for sharing).
# To share the site, run serve.py (waitress) or Docker instead.
if __name__ == "__main__":
    # with debug=True Flask starts twice; only start the scheduler in the real worker
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    app.run(debug=True, port=5001)