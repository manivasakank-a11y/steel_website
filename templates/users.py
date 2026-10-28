<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Manage users</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: "Segoe UI", Arial, sans-serif;
            margin: 0;
            padding: 20px;
            min-height: 100vh;
            background: linear-gradient(-45deg, #00b09b, #0072ff, #11998e, #38ef7d, #1e3c72);
            background-size: 400% 400%;
            animation: bgMove 15s ease infinite;
        }
        @keyframes bgMove {
            0%   { background-position: 0% 50%; }
            50%  { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .card {
            background: rgba(255, 255, 255, 0.96);
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 6px 24px rgba(0, 0, 0, 0.25);
            margin-bottom: 20px;
        }
        h1 { color: #0b3d5c; margin: 0 0 16px; }
        h2 { color: #0b3d5c; margin: 0 0 12px; font-size: 18px; }

        .userbar { display: flex; justify-content: flex-end; align-items: center; gap: 12px;
                   margin-bottom: 10px; color: #0b3d5c; font-size: 13px; }

        .btn, button {
            display: inline-block; padding: 8px 16px; border: none; border-radius: 6px;
            color: #fff; font-size: 14px; font-weight: 600; text-decoration: none; cursor: pointer;
            background: linear-gradient(135deg, #00b09b, #0072ff);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover, button:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0, 114, 255, 0.4); }
        .btn-light { background: #fff; color: #0b5d6b; border: 1px solid #0b5d6b; }
        .btn-red   { background: #c0392b; }

        .msg { padding: 10px 14px; margin-bottom: 14px; border-radius: 6px; border: 1px solid; }
        .msg.ok    { background: #e6fff3; border-color: #38ef7d; color: #0b5d3b; }
        .msg.error { background: #ffecec; border-color: #ff6b6b; color: #8a1f1f; }

        .create-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
        .create-form label { display: block; font-size: 13px; color: #333; margin-bottom: 4px; }
        input[type=text], input[type=password] {
            padding: 8px 10px; width: 210px; border: 1px solid #aaa; border-radius: 6px;
        }
        .hint { color: #555; font-size: 12px; margin-top: 10px; }

        .table-wrap { overflow-x: auto; }
        table { border-collapse: collapse; font-size: 14px; background: #fff; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 8px 10px; text-align: left; vertical-align: middle; }
        th { background: #0b5d6b; color: #fff; }
        tbody tr:nth-child(even) { background: #eef9f5; }
        .on   { color: #0b7a3b; font-weight: 600; }
        .wait { color: #b36b00; font-weight: 600; }
        .notice { padding: 10px 14px; margin-bottom: 14px; border-radius: 6px; border: 1px solid #f9d423;
                  background: #fffbe0; color: #6b5200; }
        .off { color: #b00020; font-weight: 600; }
        .badge { background: #0b5d6b; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }
        .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .actions form { display: flex; gap: 6px; margin: 0; }
        .actions input[type=password] { width: 160px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="userbar">
            <span>Logged in as <strong>{{ session.get('user') }}</strong></span>
            <a class="btn btn-light" href="{{ url_for('index') }}">Back to data</a>
            <a class="btn btn-light" href="{{ url_for('logout') }}">Log out</a>
        </div>

        <h1>Manage users</h1>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, m in messages %}
                <div class="msg {{ category }}">{{ m }}</div>
            {% endfor %}
        {% endwith %}

        {% if pending_count %}
            <div class="notice"><strong>{{ pending_count }}</strong> sign-up request(s) waiting for your approval. See the list below.</div>
        {% endif %}

        <h2>Create a new user</h2>
        <form class="create-form" method="post" action="{{ url_for('users') }}" autocomplete="off">
            <input type="hidden" name="csrf" value="{{ csrf_token() }}">
            <input type="hidden" name="action" value="create">
            <div>
                <label>User ID</label>
                <input type="text" name="username" required minlength="3" maxlength="50">
            </div>
            <div>
                <label>Password (8+ characters)</label>
                <input type="password" name="password" required minlength="8" autocomplete="new-password">
            </div>
            <div>
                <label>Repeat password</label>
                <input type="password" name="confirm" required minlength="8" autocomplete="new-password">
            </div>
            <button type="submit">Create user</button>
        </form>
        <p class="hint">User ID: letters, numbers and . _ - @ only. Give the user ID and password to the person yourself.</p>
    </div>

    <div class="card">
        <h2>All users</h2>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr><th>User ID</th><th>Status</th><th>Created</th><th>Actions</th></tr>
                </thead>
                <tbody>
                    {% for r in rows %}
                    <tr>
                        <td>{{ r.username }}{% if r.username|lower in admins %}<span class="badge">admin</span>{% endif %}</td>
                        <td>{% if r.pending and not r.active %}<span class="wait">Waiting for approval</span>{% elif r.active %}<span class="on">Active</span>{% else %}<span class="off">Disabled</span>{% endif %}</td>
                        <td>{{ r.created_at }}</td>
                        <td>
                            {% if r.pending and not r.active %}
                            <div class="actions">
                                <form method="post" action="{{ url_for('users') }}">
                                    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
                                    <input type="hidden" name="action" value="approve">
                                    <input type="hidden" name="username" value="{{ r.username }}">
                                    <button type="submit">Approve</button>
                                </form>
                                <form method="post" action="{{ url_for('users') }}">
                                    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
                                    <input type="hidden" name="action" value="reject">
                                    <input type="hidden" name="username" value="{{ r.username }}">
                                    <button type="submit" class="btn-red">Reject</button>
                                </form>
                            </div>
                            {% else %}
                            <div class="actions">
                                <form method="post" action="{{ url_for('users') }}" autocomplete="off">
                                    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
                                    <input type="hidden" name="action" value="reset">
                                    <input type="hidden" name="username" value="{{ r.username }}">
                                    <input type="password" name="password" placeholder="New password" required minlength="8" autocomplete="new-password">
                                    <button type="submit">Change password</button>
                                </form>
                                <form method="post" action="{{ url_for('users') }}">
                                    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
                                    <input type="hidden" name="username" value="{{ r.username }}">
                                    {% if r.active %}
                                        <input type="hidden" name="action" value="disable">
                                        <button type="submit" class="btn-red">Disable</button>
                                    {% else %}
                                        <input type="hidden" name="action" value="enable">
                                        <button type="submit">Enable</button>
                                    {% endif %}
                                </form>
                            </div>
                            {% endif %}
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4">No users yet.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>