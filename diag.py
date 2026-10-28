import os
import traceback

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def step(name, fn):
    print(f"\n--- {name}")
    try:
        result = fn()
        print("OK", result if result is not None else "")
        return True
    except Exception:
        print("FAILED:")
        traceback.print_exc()
        return False


print("Folder:", os.getcwd())
print("Files here:", sorted(os.listdir(".")))
print("templates:", sorted(os.listdir("templates")) if os.path.isdir("templates") else "NO templates FOLDER")
print("static:", sorted(os.listdir("static")) if os.path.isdir("static") else "NO static FOLDER")

if step("Import app.py", lambda: __import__("app") and "app.py loads"):
    import app as A
    from werkzeug.security import generate_password_hash, check_password_hash

    A.app.config["PROPAGATE_EXCEPTIONS"] = True      # show the real error instead of "Internal Server Error"

    def password_hashing():
        h = generate_password_hash("abc12345")
        return f"hash works: {check_password_hash(h, 'abc12345')}"

    def db_connect():
        c = A.get_db()
        c.close()
        return "MySQL connection works"

    first_user = {"name": None}

    def users():
        c = A.get_db()
        cur = c.cursor()
        cur.execute("SELECT username, active FROM app_users")
        rows = cur.fetchall()
        cur.close()
        c.close()
        if rows:
            first_user["name"] = A._s(rows[0][0])
        return f"users in app_users: {[(A._s(r[0]), r[1]) for r in rows]}"

    def data_table():
        c = A.get_db()
        cur = c.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {A.TABLE}")
        n = cur.fetchone()[0]
        cur.close()
        c.close()
        return f"{n} rows in {A.TABLE}"

    def get(url, user=None):
        client = A.app.test_client()
        if user:
            with client.session_transaction() as s:
                s["user"] = user
        r = client.get(url)
        return f"{url} -> HTTP {r.status_code}"

    step("Password hashing", password_hashing)
    step("Connect to MySQL", db_connect)
    step("Read app_users table", users)
    step("Read data table", data_table)
    step("Login page", lambda: get("/login"))
    step("Home page (as logged-in user)", lambda: get("/", first_user["name"] or "test"))
    step("Upload page (as logged-in user)", lambda: get("/upload", first_user["name"] or "test"))

print("\nDone. Copy everything above and send it to me.")