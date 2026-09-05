"""`horillasetup migrate hrms-v2 --from-v1` as a customer runs it.

test_external_migration.py proves the migration mechanism works. This proves
the command wrapping it does the right thing -- including, and especially, when
it should refuse.

The refusal cases carry the weight. A migration that works on a good database
but corrupts a bad one is worse than no tool, because the operator has no way
to tell which they had until much later.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import database_url, dump_path, psql

TOOL_ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = Path(os.environ.get("HORILLA_V2_ROOT", ""))
CLI = TOOL_ROOT / ".venv/bin/horillasetup"

pytestmark = pytest.mark.skipif(
    not (str(V2_ROOT) and (V2_ROOT / "manage.py").exists() and CLI.exists()),
    reason="set HORILLA_V2_ROOT to a v2 checkout, and pip install -e . in .venv",
)


def _restore(db, tag="1.6.1", suffix="_full"):
    path = dump_path(tag).replace(".dump", f"{suffix}.dump")
    if not os.path.exists(path):
        pytest.skip(f"no fixture at {path}")
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(["pg_restore", "-d", db, "--no-owner", "--no-privileges", path],
                   capture_output=True, check=False)


def _cli(db, *args):
    """Run the real console script against `db`, as a customer would."""
    env_file = V2_ROOT / ".env"
    original = env_file.read_text()
    env_file.write_text("\n".join(
        f"DATABASE_URL={database_url(db)}"
        if line.startswith("DATABASE_URL=") else line
        for line in original.splitlines()
    ))
    try:
        return subprocess.run(
            [str(CLI), "migrate", "hrms-v2", *args],
            cwd=V2_ROOT, capture_output=True, text=True,
        )
    finally:
        env_file.write_text(original)


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    """One full CLI run, shared: it takes ~3 minutes."""
    db = "cli_e2e"
    _restore(db)
    before = psql(db, "select count(*) from auth_user")
    backup_dir = tmp_path_factory.mktemp("backup")
    result = _cli(db, "--from-v1", "--yes", "--backup-dir", str(backup_dir))
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1500:]
    yield {"db": db, "before": before, "backup_dir": backup_dir,
           "output": result.stdout}
    subprocess.run(["dropdb", "--if-exists", db], check=True)


# --- the happy path -------------------------------------------------------

def test_all_six_stages_run_in_order(migrated):
    """Ordering is asserted, not just presence: stage 5 streams a subprocess
    to the same terminal, and without an explicit flush the tool's own output
    lands after it -- making the log claim migrate ran before the checks that
    gate it."""
    positions = [migrated["output"].index(f"Stage {n}/6") for n in range(1, 7)]
    assert positions == sorted(positions), "stages reported out of order"


def test_users_survive_the_command(migrated):
    assert psql(migrated["db"], "select count(*) from horilla_auth_horillauser") \
        == migrated["before"]


def test_verification_stage_actually_checks(migrated):
    assert "password hashes unchanged" in migrated["output"]
    assert "no orphaned records" in migrated["output"]


def test_backup_is_a_restorable_pre_migration_dump(migrated):
    """A file existing is not a backup. It must restore, and it must contain
    the state from BEFORE the migration -- proven by auth_user being present,
    since the migration renames it away."""
    dumps = list(Path(migrated["backup_dir"]).glob("*.dump"))
    assert dumps, "no backup was written"

    db = "cli_backup_restore"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(["pg_restore", "-d", db, "--no-owner", "--no-privileges",
                    str(dumps[0])], capture_output=True, check=False)
    try:
        assert psql(db, "select count(*) from auth_user") == migrated["before"]
        assert psql(db, "select count(*) from information_schema.tables "
                        "where table_schema='public'") == "341"
    finally:
        subprocess.run(["dropdb", "--if-exists", db], check=True)


# --- refusal --------------------------------------------------------------

def test_rerunning_on_a_migrated_database_is_refused(migrated):
    """Idempotency: a second run must not unapply the ledger and start over."""
    result = _cli(migrated["db"], "--from-v1", "--yes", "--skip-backup")
    assert result.returncode == 1
    assert "not a supported Horilla v1 schema" in result.stdout
    assert "Nothing has been changed" in result.stdout


def test_empty_database_is_refused():
    db = "cli_empty"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    try:
        result = _cli(db, "--from-v1", "--yes", "--skip-backup")
        assert result.returncode == 1
        assert "not a supported Horilla v1 schema" in result.stdout
    finally:
        subprocess.run(["dropdb", "--if-exists", db], check=True)


def test_preflight_blocks_before_writing_anything():
    """The important half of pre-flight: not just that it reports a problem,
    but that the database is provably untouched afterwards."""
    db = "cli_preflight"
    _restore(db)
    try:
        subprocess.run(["psql", "-q", "-d", db, "-c", """
            insert into base_company(id,is_active,company,hq,address,country,state,city,zip)
            values (901,true,'A',false,'x','x','x','x','1');
            insert into recruitment_recruitmentgeneralsetting
                (id,company_id_id,is_active,candidate_self_tracking,show_overall_rating)
            values (901,901,true,false,false),(902,901,true,false,false);
        """], check=True, capture_output=True)

        result = _cli(db, "--from-v1", "--yes", "--skip-backup")
        assert result.returncode == 1
        assert "recruitment_recruitmentgeneralsetting" in result.stdout

        # untouched: v1's table still there, no v2 table created
        assert psql(db, "select count(*) from information_schema.tables "
                        "where table_name='auth_user'") == "1"
        assert psql(db, "select count(*) from information_schema.tables "
                        "where table_name='base_roster'") == "0"
    finally:
        subprocess.run(["dropdb", "--if-exists", db], check=True)


def test_the_old_existing_flag_refuses_with_a_pointer():
    """--existing deleted django_migrations and faked the whole graph. It is
    refused rather than silently mapped onto the new path, so anyone with it
    in a script finds out why."""
    result = subprocess.run(
        [str(CLI), "migrate", "hrms-v2", "--existing"],
        cwd=V2_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "--from-v1" in result.stdout
    assert "deleted the migration ledger" in result.stdout


def test_running_outside_a_project_is_refused(tmp_path):
    result = subprocess.run(
        [str(CLI), "migrate", "hrms-v2", "--from-v1", "--yes"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "manage.py not found" in result.stdout
