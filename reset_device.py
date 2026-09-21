#!/usr/bin/env python3
"""
reset_device.py — clear a client's device binding

Use this when a legitimate client gets a new computer, reinstalls, or
otherwise needs their key re-bound to a new device. Their next request
will then claim the new device automatically — no need to touch
clients.json at all, this only affects device_bindings.json.

Run this from inside your jde-server folder:
    python3 reset_device.py
"""

import json
import os
import sys

CLIENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients.json")
BINDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_bindings.json")


def load_json(path, label):
    if not os.path.exists(path):
        print(f"No {label} found at {path}.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: {label} is not valid JSON ({e}). Fix it manually first.")
            sys.exit(1)


def save_json_atomically(path, data):
    serialized = json.dumps(data, indent=2)
    json.loads(serialized)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
    os.replace(tmp_path, path)


def main():
    print("=== Reset a client's device binding ===\n")

    clients = load_json(CLIENTS_PATH, "clients.json")
    bindings = load_json(BINDINGS_PATH, "device_bindings.json")

    if not bindings:
        print("No device bindings exist yet — nothing to reset.")
        return

    # Show which clients currently have an active binding, by name, so
    # you don't have to hunt for the raw API key yourself.
    print("Clients with an active device binding:")
    key_to_name = {k: v.get("client_name", "unknown") for k, v in clients.items()}
    bound_entries = []
    for api_key, device_id in bindings.items():
        name = key_to_name.get(api_key, "(key not found in clients.json)")
        bound_entries.append((api_key, name, device_id))
        print(f"  {name}  —  device {device_id[:8]}...  —  key ending {api_key[-8:]}")

    if not bound_entries:
        print("(none)")
        return

    target = input("\nEnter the client's name (or the last 8 characters of their key) to reset: ").strip()

    matches = [
        (k, n, d) for k, n, d in bound_entries
        if target.lower() in n.lower() or k.endswith(target)
    ]

    if not matches:
        print(f"No binding found matching '{target}'.")
        return
    if len(matches) > 1:
        print(f"'{target}' matches more than one client — be more specific:")
        for k, n, d in matches:
            print(f"  {n}  —  key ending {k[-8:]}")
        return

    api_key, name, device_id = matches[0]
    confirm = input(f"Reset device binding for '{name}' (key ending {api_key[-8:]})? Type 'yes' to confirm: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled — nothing was changed.")
        return

    del bindings[api_key]
    save_json_atomically(BINDINGS_PATH, bindings)
    print(f"\nDone. '{name}' can now connect from a new device — their next request will claim it automatically.")
    print("Remember to sync the updated device_bindings.json to Render if it lives there as a Secret File / persistent disk.")


if __name__ == "__main__":
    main()
