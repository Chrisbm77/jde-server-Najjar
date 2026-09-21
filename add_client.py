#!/usr/bin/env python3
"""
add_client.py — safely add one or more clients to clients.json

Run this from inside your jde-server folder:
    python3 add_client.py

Asks for a department and database connection ONCE (these are almost
always shared by everyone in the same department), then loops asking
for names — one API key generated per name, all added together.
Validates the file before AND after, and writes atomically (temp file +
rename) so a crash partway through can never corrupt the file for every
other client already in it.
"""

import json
import os
import secrets
import sys
import datetime
import getpass

CLIENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients.json")


def load_departments():
    """Pull the valid department list directly from config.py, so this
    script can never drift out of sync with what's actually configured."""
    try:
        import config
        return sorted(set(getattr(config, "DEPARTMENT_TABLES", {}).keys())
                       | set(getattr(config, "DEPARTMENT_TABLE_PREFIXES", {}).keys()))
    except Exception as e:
        print(f"WARNING: could not import config.py to check valid departments ({e}).")
        print("Continuing without department validation — double-check spelling yourself.")
        return None


def load_clients():
    if not os.path.exists(CLIENTS_PATH):
        print(f"No clients.json found at {CLIENTS_PATH} — starting a new one.")
        return {}
    with open(CLIENTS_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: existing clients.json is not valid JSON ({e}).")
            print("Fix it manually before running this script — refusing to touch a broken file.")
            sys.exit(1)


def save_clients_atomically(clients: dict):
    serialized = json.dumps(clients, indent=2)
    json.loads(serialized)  # will raise if something went wrong building it

    tmp_path = CLIENTS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
    os.replace(tmp_path, CLIENTS_PATH)  # atomic on the same filesystem


def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if not value and not required:
            return ""
        if value:
            return value
        print("This is required — please enter a value.")


def collect_names():
    print("\nEnter each person's name, one at a time. Press Enter on a blank line when done.")
    names = []
    while True:
        name = input(f"  Person {len(names) + 1} (blank to finish): ").strip()
        if not name:
            if not names:
                print("  Add at least one person.")
                continue
            break
        names.append(name)
    return names


def main():
    print("=== Add client(s) to JDE Assistant ===\n")

    clients = load_clients()
    departments = load_departments()

    if departments:
        print(f"Valid departments: {', '.join(departments)}")
        print("(leave blank for full-access / admin keys, no department restriction)")
    department = ask("Department for this batch", default="", required=False)
    if department and departments and department not in departments:
        print(f"\nWARNING: '{department}' is not in the known department list.")
        confirm = ask("Type this exact department again to confirm you really mean it", required=True)
        if confirm != department:
            print("Department names didn't match — aborting. Run the script again.")
            sys.exit(1)

    expires_on = ask("Expiry date for this batch (YYYY-MM-DD), or leave blank for no expiry", default="", required=False)
    if expires_on:
        try:
            datetime.date.fromisoformat(expires_on)
        except ValueError:
            print(f"'{expires_on}' isn't a valid YYYY-MM-DD date — aborting.")
            sys.exit(1)

    print("\n--- Database connection for this batch ---")
    print("(This is almost always the same for everyone in one department —")
    print(" answer it once here, it'll apply to every person you add below.)")
    dsn = ask("DSN (host:port/service_name)")
    db_user = ask("Database username")
    db_password = getpass.getpass("Database password (hidden as you type): ")

    names = collect_names()

    new_entries = {}
    for name in names:
        api_key = "sk_live_" + secrets.token_hex(32)
        while api_key in clients or api_key in new_entries:
            api_key = "sk_live_" + secrets.token_hex(32)  # collision is astronomically unlikely, but guard anyway
        entry = {
            "client_name": name,
            "active": True,
            "expires_on": expires_on if expires_on else None,
            "db": {"dsn": dsn, "user": db_user, "password": db_password},
        }
        if department:
            entry["department"] = department
        new_entries[api_key] = entry

    print(f"\n--- About to add {len(new_entries)} client(s) ---")
    for api_key, entry in new_entries.items():
        preview = dict(entry)
        preview["db"] = dict(preview["db"])
        preview["db"]["password"] = "*" * len(db_password)
        print(f"\n{entry['client_name']}:")
        print(json.dumps({api_key: preview}, indent=2))

    confirm = ask(f"\nType 'yes' to save these {len(new_entries)} client(s) to clients.json", required=True)
    if confirm.lower() != "yes":
        print("Cancelled — nothing was saved.")
        sys.exit(0)

    clients.update(new_entries)
    save_clients_atomically(clients)

    print(f"\nSaved. clients.json now has {len(clients)} client(s) total.")
    print("\n=== IMPORTANT — save these keys now, they will not be shown again ===")
    for api_key, entry in new_entries.items():
        print(f"{entry['client_name']}: {api_key}")
    print("========================================================================")
    print("\nNext steps:")
    print("1. Send each key to its person through a secure channel (not this terminal's scrollback).")
    print("2. If clients.json lives on Render as a Secret File, upload this updated version there too.")
    print("3. Give each person the standard install package (same one everyone uses) plus their own key.")


if __name__ == "__main__":
    main()
