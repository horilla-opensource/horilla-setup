"""Migrate an existing Horilla v1 database to v2, in place.

Six stages, in this order, because each protects the next:

    1 FINGERPRINT   is this really a supported v1 database?
    2 PRE-FLIGHT    is there data that would make the migration fail partway?
    3 BACKUP        can this be undone?
    4 LEDGER        reconcile django_migrations with v2's migration files
    5 MIGRATE       apply v2's schema over v1's, adopting what exists
    6 VERIFY        did the data actually survive?

Nothing before stage 3 writes to the database. Stage 3 is what makes stages
4-6 recoverable, so --skip-backup exists but warns.

Everything runs as a subprocess inside the project directory rather than
importing Horilla into this interpreter. The tool is installed globally, often
outside any project's virtualenv, so it cannot import Django itself -- and must
not, since the project's own interpreter is the one with the right versions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Applied over the project's own settings, adding only MIGRATION_MODULES.
SETTINGS_MODULE = "horillasetup.migration_settings"
TOOL_ROOT = str(Path(__file__).resolve().parent.parent)

SUPPORTED_RANGE = "1.3.2 - 1.6.1"


class MigrationError(Exception):
    """Anything that should stop the migration with a readable message."""


def _project_python(project: Path) -> str:
    """The project's interpreter, not the tool's.

    A globally-installed tool runs under a Python that has none of Horilla's
    dependencies, so every subprocess must use the project's venv.
    """
    for candidate in (project / ".venv/bin/python", project / "venv/bin/python",
                      project / ".venv/Scripts/python.exe"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _env(project: Path, adopt: bool = False) -> dict:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": SETTINGS_MODULE}
    # Prepend rather than replace: the project may already need its own entries.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{TOOL_ROOT}{os.pathsep}{existing}" if existing else TOOL_ROOT
    if adopt:
        env["HORILLA_ADOPT_EXISTING_SCHEMA"] = "1"
    return env


def _run_script(project: Path, body: str, adopt: bool = False):
    """Run a snippet inside the project, with Django configured.

    Written to a file in the project directory rather than passed with -c:
    Django's settings import needs the project itself on sys.path, and running
    from anywhere else fails with ModuleNotFoundError: No module named 'horilla'.
    """
    script = project / "_horillasetup_stage.py"
    script.write_text("import django\ndjango.setup()\n" + body)
    try:
        return subprocess.run(
            [_project_python(project), script.name],
            cwd=project, env=_env(project, adopt),
            capture_output=True, text=True,
        )
    finally:
        script.unlink(missing_ok=True)


def _emit(result) -> dict:
    """Parse `KEY value` lines out of a stage's stdout.

    Django logs freely to stdout during setup (axes, LDAP warnings), so stages
    mark their own output rather than the caller parsing everything.
    """
    data = {}
    for line in result.stdout.splitlines():
        if not line.startswith("::"):
            continue
        key, _, value = line[2:].partition(" ")
        data[key] = value.strip()
    return data


# --- stage 1 ---------------------------------------------------------------

def fingerprint(project: Path) -> dict:
    """Identify the database before touching it.

    Refusing an unrecognised schema is the whole point: migrating one on a
    guess is how a database gets silently corrupted, and the operator has no
    way to tell until much later.
    """
    result = _run_script(project, """
from django.db import connection
from horillasetup.migration.fingerprint import fingerprint
fp = fingerprint(connection)
print("::variant", fp.variant)
print("::supported", fp.supported)
print("::tables", fp.table_count)
print("::detail", fp.describe())
""")
    if result.returncode != 0:
        raise MigrationError(
            f"could not inspect the database:\n{result.stderr[-1500:]}"
        )
    return _emit(result)


# --- stage 2 ---------------------------------------------------------------

def preflight(project: Path) -> list:
    """Find data that would make the migration fail halfway through.

    Failing before any write is recoverable; failing at migration 90 of 190
    leaves a half-changed schema.
    """
    result = _run_script(project, """
from django.db import connection
from horillasetup.migration.fingerprint import preflight
for problem in preflight(connection):
    print("::problem", problem)
""")
    if result.returncode != 0:
        raise MigrationError(f"pre-flight failed:\n{result.stderr[-1500:]}")
    return [line[len("::problem "):] for line in result.stdout.splitlines()
            if line.startswith("::problem ")]


# --- stage 3 ---------------------------------------------------------------

def backup(project: Path, backup_dir: Path) -> Path:
    """pg_dump the whole database before anything is written.

    Uses the project's own DATABASES setting rather than asking for connection
    details, so the backup cannot end up pointing at a different database than
    the one being migrated.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"horilla-v1-{stamp}.dump"

    result = _run_script(project, f"""
from django.conf import settings
db = settings.DATABASES["default"]
print("::name", db["NAME"])
print("::user", db.get("USER") or "")
print("::host", db.get("HOST") or "localhost")
print("::port", db.get("PORT") or "5432")
print("::password", db.get("PASSWORD") or "")
""")
    if result.returncode != 0:
        raise MigrationError(f"could not read database settings:\n{result.stderr[-1500:]}")
    conf = _emit(result)

    if not conf.get("name"):
        raise MigrationError("no database configured in settings.DATABASES")

    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
           "--file", str(target), "--dbname", conf["name"]]
    if conf.get("user"):
        cmd += ["--username", conf["user"]]
    if conf.get("host"):
        cmd += ["--host", conf["host"]]
    if conf.get("port"):
        cmd += ["--port", conf["port"]]

    env = {**os.environ}
    if conf.get("password"):
        env["PGPASSWORD"] = conf["password"]

    dump = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if dump.returncode != 0:
        raise MigrationError(
            "pg_dump failed, so the migration cannot be undone -- refusing to "
            f"continue:\n{dump.stderr[-1500:]}"
        )
    if not target.exists() or target.stat().st_size == 0:
        raise MigrationError(f"backup at {target} is empty")
    return target


# --- stage 4 ---------------------------------------------------------------

def reconcile_ledger(project: Path) -> dict:
    """Make django_migrations describe what v2 still needs to apply.

    Two distinct problems, both of which make `migrate` do the wrong thing:

    Name collisions -- v1 and v2 both auto-generated migrations with Django's
    default names, so a v1 ledger already claims `base.0001_initial` and 25
    others. Django sees the name as applied and skips v2's entirely different
    migration of the same name, so new tables are never created while the
    ledger reports success.

    Ordering -- v2 swaps AUTH_USER_MODEL, so much of the graph now depends on
    horilla_auth, which a v1 ledger records as applied *after* them.
    """
    result = _run_script(project, """
from django.db import connection
from horillasetup.migration.adopt import (
    unapply_colliding_ledger_rows, clear_auth_ordering_conflicts,
)
print("::collisions", len(unapply_colliding_ledger_rows(connection)))
print("::ordering", len(clear_auth_ordering_conflicts(connection)))
""")
    if result.returncode != 0:
        raise MigrationError(f"ledger reconciliation failed:\n{result.stderr[-1500:]}")
    return _emit(result)


# --- stage 5 ---------------------------------------------------------------

def migrate(project: Path) -> None:
    """Apply v2's migrations with schema adoption active.

    Streams output rather than capturing it: this is the slow stage (~2
    minutes) and silence here reads as a hang.
    """
    # This subprocess writes straight to the terminal. Without flushing first,
    # everything this tool has printed so far is still buffered and lands after
    # it -- making the log read as though migrate ran before the stages that
    # gate it.
    sys.stdout.flush()
    result = subprocess.run(
        [_project_python(project), "manage.py", "migrate", "--noinput"],
        cwd=project, env=_env(project, adopt=True),
    )
    if result.returncode != 0:
        raise MigrationError(
            "migrate failed. The database is part-migrated; restore the backup "
            "from stage 3 before retrying."
        )


# --- stage 6 ---------------------------------------------------------------

def verify(project: Path, before: dict) -> list:
    """Confirm the data survived. A clean `migrate` is not proof of that.

    Compares against counts taken before the migration, so this catches loss
    rather than merely asserting the schema looks plausible.
    """
    result = _run_script(project, """
from django.db import connection
with connection.cursor() as c:
    def scalar(sql):
        c.execute(sql)
        row = c.fetchone()
        return row[0] if row else 0

    print("::users", scalar("select count(*) from horilla_auth_horillauser"))
    print("::orphans", scalar(
        "select count(*) from employee_employee e "
        "where e.employee_user_id_id is not null and not exists ("
        "  select 1 from horilla_auth_horillauser u where u.id = e.employee_user_id_id)"))
    print("::stale_fks", scalar(
        "select count(*) from information_schema.table_constraints tc "
        "join information_schema.constraint_column_usage ccu "
        "  on tc.constraint_name = ccu.constraint_name "
        "where tc.constraint_type = 'FOREIGN KEY' and ccu.table_name = 'auth_user'"))
    print("::hashes", scalar(
        "select coalesce(md5(string_agg(username||password, ',' order by username)), '') "
        "from horilla_auth_horillauser"))
""")
    if result.returncode != 0:
        raise MigrationError(f"verification could not run:\n{result.stderr[-1500:]}")
    after = _emit(result)

    problems = []
    if int(after.get("users", 0)) != int(before.get("users", 0)):
        problems.append(
            f"user count changed: {before.get('users')} before, "
            f"{after.get('users')} after"
        )
    if int(after.get("orphans", 0)):
        problems.append(
            f"{after['orphans']} employee(s) no longer resolve to a user"
        )
    if int(after.get("stale_fks", 0)):
        problems.append(
            f"{after['stale_fks']} foreign key(s) still point at auth_user"
        )
    if before.get("hashes") and after.get("hashes") != before.get("hashes"):
        problems.append(
            "password hashes changed -- users would be unable to log in"
        )
    return problems


def snapshot(project: Path) -> dict:
    """Counts taken before the migration, for stage 6 to compare against.

    Reads v1's table names, since this runs before the rename.
    """
    result = _run_script(project, """
from django.db import connection
with connection.cursor() as c:
    def scalar(sql):
        c.execute(sql)
        row = c.fetchone()
        return row[0] if row else 0
    print("::users", scalar("select count(*) from auth_user"))
    print("::hashes", scalar(
        "select coalesce(md5(string_agg(username||password, ',' order by username)), '') "
        "from auth_user"))
""")
    if result.returncode != 0:
        raise MigrationError(f"could not read the database:\n{result.stderr[-1500:]}")
    return _emit(result)


# --- orchestration ---------------------------------------------------------

def run(project: Path, backup_dir: Path | None = None,
        skip_backup: bool = False, assume_yes: bool = False) -> int:
    """Run all six stages. Returns a process exit code.

    """
    project = Path(project).resolve()
    if not (project / "manage.py").exists():
        print("⚠️  manage.py not found. Run this from a Horilla v2 project "
              "directory.\n")
        return 1

    print("\n🔍 Stage 1/6 — identifying the database\n")
    fp = fingerprint(project)
    print(f"   {fp.get('detail', 'unknown')}")
    if fp.get("supported") != "True":
        print(
            f"\n❌ This database is not a supported Horilla v1 schema "
            f"({fp.get('variant', 'unknown')}).\n"
            f"   Supported source versions: {SUPPORTED_RANGE}.\n"
            "   Nothing has been changed.\n"
        )
        return 1
    print("   ✅ supported\n")

    print("🔍 Stage 2/6 — pre-flight checks\n")
    problems = preflight(project)
    if problems:
        print("❌ The migration would fail partway through:\n")
        for problem in problems:
            print(f"   • {problem}")
        print("\n   Fix these and run again. Nothing has been changed.\n")
        return 1
    print("   ✅ no blocking data found\n")

    before = snapshot(project)
    print(f"   {before.get('users', '?')} users to migrate\n")

    if not assume_yes:
        print("This rewrites the database in place. A backup is taken first"
              if not skip_backup else
              "This rewrites the database in place, WITHOUT a backup")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("\nAborted. Nothing has been changed.\n")
            return 1

    if skip_backup:
        print("\n⚠️  Stage 3/6 — SKIPPED. This cannot be undone.\n")
    else:
        target = backup_dir or (project / "horilla-migration-backups")
        print(f"\n💾 Stage 3/6 — backing up to {target}\n")
        written = backup(project, Path(target))
        size = written.stat().st_size / (1024 * 1024)
        print(f"   ✅ {written.name} ({size:.1f} MB)\n")

    print("📒 Stage 4/6 — reconciling the migration ledger\n")
    ledger = reconcile_ledger(project)
    print(f"   {ledger.get('collisions', '0')} colliding migration names unapplied")
    print(f"   {ledger.get('ordering', '0')} rows reordered for the new user model\n")

    print("🚀 Stage 5/6 — applying v2 migrations (this takes a few minutes)\n")
    migrate(project)

    print("\n🔎 Stage 6/6 — verifying\n")
    failures = verify(project, before)
    if failures:
        print("❌ The migration completed but the data did not survive intact:\n")
        for failure in failures:
            print(f"   • {failure}")
        print("\n   Restore the backup from stage 3.\n")
        return 1

    print(f"   ✅ {before.get('users', '?')} users, no orphaned records, "
          "password hashes unchanged\n")
    print("✅ Migration complete. Existing users can log in with their "
          "current passwords.\n")
    return 0
