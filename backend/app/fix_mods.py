"""Refresh snapshot mod columns from their own raw payload, dry-run by default.

Never rewrites historical Event rows, whose original payloads are unavailable.
"""

import argparse
import json
from . import create_app, db
from .models import ItemSnapshot
from .items import extract_mods, MOD_FIELDS


def fix(apply=False):
    changed = 0
    with create_app().app_context():
        for row in ItemSnapshot.query.yield_per(200):
            mods = extract_mods((row.item_data or {}).get("item", {}))
            values = {
                column: json.dumps(mods[key], ensure_ascii=False)
                for key, column in MOD_FIELDS.items()
            }
            if any(getattr(row, key) != value for key, value in values.items()):
                changed += 1
                if apply:
                    for key, value in values.items():
                        setattr(row, key, value)
        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    print(
        f"mode={'apply' if apply else 'dry-run'} changed_snapshots={changed} historical_events_untouched=true"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    fix(parser.parse_args().apply)
