import json
from datetime import timedelta
from pathlib import Path

from app import auth, clock, db

DEMO_PASSWORD = "demo1234"
USERS = [
    ("analyst", "Priya Nair", "analyst"),
    ("manager", "Rahul Mehta", "manager"),
    ("head", "Anita Desai", "regional_head"),
    ("admin", "Vikram Rao", "admin"),
]
INVENTORY = [
    ("DU-500", "display unit", "Pune Warehouse", 120),
    ("DU-500", "display unit", "Mumbai Warehouse", 260),
    ("DU-500", "display unit", "Bengaluru Warehouse", 40),
    ("POS-10", "POS terminal", "Pune Warehouse", 6),
    ("SH-200", "shelving rack", "Pune Warehouse", 35),
]
SUPPLIERS = [
    ("Kraft Displays Pvt Ltd", "orders@kraftdisplays.example", 10),
    ("Metro Fixtures", "sales@metrofixtures.example", 4),
    ("QuickRack Solutions", "support@quickrack.example", 7),
]


def run() -> None:
    today = clock.now().date()
    db.set_setting("seeded_on", today.isoformat())
    for username, name, role in USERS:
        db.execute("INSERT INTO users(username, name, role, pw_hash) VALUES (?,?,?,?)",
                   (username, name, role, auth.hash_password(DEMO_PASSWORD)))
    stores = [
        ("Pune - Phoenix Marketcity", "Pune", today + timedelta(days=6), "planned"),
        ("Mumbai - Andheri", "Mumbai", today - timedelta(days=400), "open"),
        ("Bengaluru - Indiranagar", "Bengaluru", today + timedelta(days=20), "planned"),
    ]
    for name, city, launch, status in stores:
        db.execute("INSERT INTO stores(name, city, launch_date, status) VALUES (?,?,?,?)",
                   (name, city, launch.isoformat(), status))
    for row in INVENTORY:
        db.execute("INSERT INTO inventory(sku, item, location, qty) VALUES (?,?,?,?)", row)
    for row in SUPPLIERS:
        db.execute("INSERT INTO suppliers(name, contact, lead_time_days) VALUES (?,?,?)", row)

    emails = json.loads((Path(__file__).parent.parent / "data" / "sample_emails.json").read_text())
    for e in emails:
        hour, minute = map(int, e["time"].split(":"))
        received = clock.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        db.execute(
            "INSERT INTO inbox(sender, subject, body, received_at, channel, request_id) VALUES (?,?,?,?,?,NULL)",
            (e["sender"], e["subject"], e["body"], clock.iso(received), "email"))
