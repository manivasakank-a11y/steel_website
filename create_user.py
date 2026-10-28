import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from werkzeug.security import generate_password_hash
from app import get_db

print("=== Create (or reset) a website user ===")
username = input("New user ID: ").strip()
password = input("Password - at least 8 characters (you can SEE it while typing): ").strip()

if not username:
    print("\nUser ID is empty. Run the script again.")
    raise SystemExit(1)

if len(password) < 8:
    print(f"\nPassword has only {len(password)} characters. It needs at least 8. Run the script again.")
    raise SystemExit(1)

conn = get_db()
cur = conn.cursor()
try:
    cur.execute("SELECT id FROM app_users WHERE username = %s", (username,))
    exists = cur.fetchone() is not None
    pw_hash = generate_password_hash(password)

    if exists:
        cur.execute("UPDATE app_users SET password_hash = %s, active = 1 WHERE username = %s",
                    (pw_hash, username))
        result = "already existed, so its password was updated"
    else:
        cur.execute("INSERT INTO app_users (username, password_hash) VALUES (%s, %s)",
                    (username, pw_hash))
        result = "was created"
    conn.commit()
    print(f"\nDone. User '{username}' {result}. You can now log in.")
    print("Tip: type  cls  and press Enter to clear the screen so nobody sees the password.")
except Exception as e:
    conn.rollback()
    print(f"\nFailed: {e}")
finally:
    cur.close()
    conn.close()