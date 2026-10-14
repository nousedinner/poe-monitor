"""Legacy command alias for the versioned schema upgrade.

Historical event data is not reconstructed from current snapshots.
Use python manage.py upgrade; review migration and backup guidance first.
"""

from . import create_app
from .schema import upgrade


def migrate():
    with create_app().app_context():
        upgrade()


if __name__ == "__main__":
    migrate()
