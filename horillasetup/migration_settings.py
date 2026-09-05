"""Horilla's own settings, plus the one key that relocates the auth migration.

Used as DJANGO_SETTINGS_MODULE for the duration of a v1 -> v2 migration:

    DJANGO_SETTINGS_MODULE=horillasetup.migration_settings \
    PYTHONPATH=/path/to/horilla-setup \
      python manage.py migrate

The point is that the HR codebase is not edited at all. MIGRATION_MODULES lets
horilla_auth's migrations be served from this package instead of from the app
directory, which is what lets the tool own the adoption logic. Django keys
migrations by (app_label, name), not by module path, so nothing else notices.

Everything else is inherited verbatim -- INSTALLED_APPS, DATABASES, the lot --
so a customer's own local_settings.py overrides still apply.
"""

import os as _os

# The project's real settings. Overridable for a project that has renamed
# them, which the settings package itself does not assume either.
_base = _os.environ.get("HORILLA_BASE_SETTINGS", "horilla.settings")

# A star-import, not importlib + vars(): Django reads settings as module-level
# names, and this is the same mechanism horilla/settings/__init__.py already
# uses on horilla/settings/base.py. Checked for hostility to it first -- the
# package defines no __all__ and no module __getattr__, so nothing is hidden.
exec(f"from {_base} import *")  # noqa: F403,S102

# Serve horilla_auth's migrations from this package. The migration there
# adopts a v1 auth_user table by renaming it, and creates the table normally
# on a fresh install -- see horillasetup/migrations/horilla_auth/0001_initial.py
MIGRATION_MODULES = {"horilla_auth": "horillasetup.migrations.horilla_auth"}

# Make CreateModel idempotent for the duration of this run, so v2 migrations
# can be applied over a schema where most of their tables already exist.
#
# Here rather than in the HR codebase because Django imports settings before
# it loads any migration, which is the hook the previous design got by editing
# horilla/__init__.py. Gated on HORILLA_ADOPT_EXISTING_SCHEMA, so importing
# these settings without the flag behaves exactly like the project's own.
from horillasetup.migration.adopt import install as _install_adoption  # noqa: E402

_install_adoption()
