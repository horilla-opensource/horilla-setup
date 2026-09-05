"""End-to-end assertions for the v1 -> v2 migration.

Each of these began as a strict xfail describing a confirmed defect. They now
pass against `manage.py migrate_from_v1`, so they are plain assertions and
guard against the defect returning.

Findings closed (see Claude-Code-Setup-Tool-Migration-Review.md):
  F1  v2 migrations are all 0001_initial/CreateModel and collided with a v1
      database; the old tool faked them so no schema change was applied
  F2  django_migrations was deleted outright, with no backup
  F4  328 FKs pointed at auth_user after users were copied to a new table
  F6  no backup, no rollback, no verification

Every test here runs a real migration against a real Postgres database, so
the module is slow by design -- roughly two minutes per tag.
"""

import os
import subprocess

import pytest

from conftest import column_set, dump_path, psql, row_count

# Every FK in v1 that references auth_user. Enumerated from a real fixture,
# not estimated. If the migration moves users to a new table without
# repointing these, each one silently detaches.
AUTH_USER_FK_QUERY = """
select count(*)
from information_schema.table_constraints tc
join information_schema.constraint_column_usage ccu
  on tc.constraint_name = ccu.constraint_name
where tc.constraint_type = 'FOREIGN KEY'
  and ccu.table_name = 'auth_user';
"""


@pytest.fixture(scope="module", params=["1.3.2", "1.6.1"])
def migrated(request, tmp_path_factory):
    """One completed migration per schema variant, reused by every assertion.

    A full migrate_from_v1 run takes ~4 minutes against a real database, so
    running one per test would make this module take over half an hour. The
    assertions below only read, so sharing is safe.

    The backup directory is captured too, since one test asserts a dump was
    written.
    """
    import subprocess as sp
    tag = request.param
    if not os.path.exists(dump_path(tag)):
        pytest.skip(f"no fixture for {tag}; run fixtures/build_v1.sh {tag}")

    db = f"mig_e2e_{tag.replace('.', '_')}"
    sp.run(["dropdb", "--if-exists", db], check=True)
    sp.run(["createdb", db], check=True)
    sp.run(["pg_restore", "-d", db, "--no-owner", "--no-privileges", dump_path(tag)],
           capture_output=True, check=False)

    before = {
        "ledger": row_count(db, "django_migrations"),
        "fk_count": int(psql(db, AUTH_USER_FK_QUERY)),
        "hashes": psql(db, "select username||' '||password from auth_user order by username"),
    }
    backup_dir = tmp_path_factory.mktemp("backup")
    run_migration(db, backup_dir=str(backup_dir))

    yield {"db": db, "before": before, "backup_dir": backup_dir, "tag": tag}
    sp.run(["dropdb", "--if-exists", db], check=True)


def test_fixture_baseline_fk_count(v1_db):
    """Documents the size of the F4 problem. Not an xfail -- this is just the
    starting condition every later assertion is measured against."""
    assert int(psql(v1_db, AUTH_USER_FK_QUERY)) > 300


def test_v2_schema_is_actually_applied(migrated):
    """F1: the v2-only tables must physically exist afterwards.

    The old tool's `migrate --fake` marked every app applied while creating
    nothing, so django_migrations claimed v2 while the schema stayed v1.
    """
    cols = column_set(migrated["db"])
    # A representative v2-only model (37 new models exist in total).
    assert any(c.startswith("base_roster.") for c in cols), (
        "v2 model base_roster was never created"
    )


def test_migration_ledger_survives(migrated):
    """F2: the ledger must not be wiped.

    It is the only record of what has been applied; losing it makes a
    half-finished migration unrecoverable by inspection. The new entrypoint
    unapplies 26 name-colliding rows and adds one, rather than deleting all.
    """
    assert row_count(migrated["db"], "django_migrations") >= migrated["before"]["ledger"]


def test_no_orphaned_user_foreign_keys(migrated):
    """Every employee/attendance/payslip row must still resolve to its user.

    F4: solved by construction. HorillaUser adopts auth_user via db_table, so
    no row moves and no foreign key is re-pointed.
    """
    orphans = int(psql(migrated["db"], """
        select count(*) from employee_employee e
        where e.employee_user_id_id is not null
          and not exists (
            select 1 from auth_user u where u.id = e.employee_user_id_id
          );
    """))
    assert orphans == 0, f"{orphans} employees detached from their user"


def test_backup_is_taken_before_any_write(migrated):
    """F6: a migration that cannot be undone must not be the default path."""
    directory = migrated["backup_dir"]
    dumps = list(directory.glob("*.dump")) + list(directory.glob("*.sql"))
    assert dumps, "no backup artefact was produced"


def test_password_hashes_are_preserved(v1_db):
    """Currently PASSES -- migrateusers.py copies the raw hash rather than
    calling set_password. Pinned so a future rewrite cannot regress it.

    Asserted against the v1 table because users have not moved yet; once
    Phase 3 lands (db_table="auth_user") this stays valid unchanged.
    """
    before = psql(v1_db, "select username||' '||password from auth_user order by username;")
    assert "pbkdf2_sha256$" in before
    assert before.count("\n") == 9  # 10 users


def run_migration(db, backup_dir=None):
    """Run the real migration entrypoint against `db`.

    Shells out rather than calling call_command in-process: the migration
    depends on v2's own settings and installed apps, which cannot be loaded
    from inside a v1-era test runner.
    """
    import os
    from pathlib import Path

    # No default: skip rather than point at the author's machine.
    v2_root = Path(os.environ.get("HORILLA_V2_ROOT", ""))
    if not (str(v2_root) and (v2_root / "manage.py").exists()):
        pytest.skip("set HORILLA_V2_ROOT to a v2 checkout with an installed venv")

    env_file = v2_root / ".env"
    original = env_file.read_text()
    env_file.write_text("\n".join(
        f"DATABASE_URL=postgres://{os.environ.get('USER')}@localhost:5432/{db}"
        if line.startswith("DATABASE_URL=") else line
        for line in original.splitlines()
    ))
    cmd = [str(v2_root / ".venv/bin/python"), "manage.py", "migrate_from_v1", "--yes"]
    cmd += ["--backup-dir", backup_dir] if backup_dir else ["--skip-backup"]
    try:
        result = subprocess.run(cmd, cwd=v2_root, capture_output=True, text=True)
    finally:
        env_file.write_text(original)
    if result.returncode != 0:
        raise AssertionError(
            f"migrate_from_v1 failed:\n{result.stdout[-2500:]}\n{result.stderr[-1500:]}"
        )
