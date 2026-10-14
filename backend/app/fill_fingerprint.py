"""Conservative historical fingerprint repair; dry-run unless --apply is set.

Only a matching current raw snapshot can establish the missing source metadata.
Unverifiable historical events are reported and left unchanged.
"""

import argparse
from . import create_app, db
from .models import Event, ItemSnapshot
from .items import fingerprint, extract_mods, MOD_FIELDS, mod_text
import json

compute_fingerprint_hash = fingerprint


def main(apply=False):
    matched = skipped = 0
    with create_app().app_context():
        after_id = 0
        while True:
            # Buffer each page before issuing more queries on a MySQL connection.
            batch = (
                Event.query.filter(Event.fingerprint_hash.is_(None), Event.id > after_id)
                .order_by(Event.id)
                .limit(200)
                .all()
            )
            if not batch:
                break
            after_id = batch[-1].id
            for event in batch:
                snapshot = ItemSnapshot.query.filter_by(
                    target_id=event.target_id, item_id=event.item_id
                ).first()
                info = (snapshot.item_data or {}).get("item", {}) if snapshot else {}
                same = (
                    info.get("name") == event.item_name and info.get("typeLine") == event.base_type
                )
                mods = extract_mods(info)
                for key, column in MOD_FIELDS.items():
                    try:
                        stored = json.loads(getattr(event, column) or "[]")
                        same = (
                            same
                            and isinstance(stored, list)
                            and sorted(mod_text(v) for v in stored) == sorted(mods[key])
                        )
                    except (ValueError, TypeError):
                        same = False
                if not same or not snapshot:
                    skipped += 1
                    continue
                matched += 1
                if apply:
                    event.fingerprint_hash = fingerprint(info)
        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    print(
        f"mode={'apply' if apply else 'dry-run'} verified={matched} skipped_unverifiable={skipped}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
