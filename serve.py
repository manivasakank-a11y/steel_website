import os
import socket

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from waitress import serve
from app import app, start_scheduler

# Running on your PC:  HOST defaults to 127.0.0.1 (only this PC - use this with the public tunnel link)
#                      set  $env:HOST = "0.0.0.0"  to also allow other PCs in your office network
# Running in Docker:   docker-compose.yml sets HOST=0.0.0.0 (needed inside a container)
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))       # port 80 is blocked on this PC
IN_DOCKER = os.path.exists("/.dockerenv")


def lan_ip():
    """This PC's address inside the office network (for colleagues)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))     # nothing is sent, it only picks the network card
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


start_scheduler()

print("=" * 64)
if IN_DOCKER:
    print(f" Running in Docker on port {PORT}")
    print(f" Open on the host PC: http://localhost:{PORT}")
else:
    print(f" On this PC      : http://heatnumbertracking:{PORT}")
    print(f"                   http://127.0.0.1:{PORT}")
    if HOST == "0.0.0.0":
        print(f" Office network  : http://{lan_ip()}:{PORT}")
    print(" Keep this window open. Close it only when you want to stop the site.")
print("=" * 64, flush=True)

serve(app, host=HOST, port=PORT, threads=8)