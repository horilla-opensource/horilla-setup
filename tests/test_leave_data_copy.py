"""base/0015 must carry Holiday and CompanyLeave rows across the app move.

v2 moves both models from `leave` to `base` with a CreateModel in base and a
DeleteModel in leave, and no data step between them. Without base/0015 every
holiday and company-leave rule a customer configured is silently destroyed --
confirmed against a real 1.6.1 database before the fix: 1 row in, 0 out.

These tests need the HR fixture (companies, employees, a holiday), not the
plain user fixture:

    tests/fixtures/build_v1.sh 1.6.1
    # then seed HR data -- see fixtures/seed_v1_hr_data.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import psql  # noqa: E402

HR_DUMP = os.environ.get(
    "HORILLA_V1_HR_DUMP", "/tmp/horilla-v1-fixtures/v1_1.6.1_full.dump"
)
# A v2 checkout with an installed venv, used to run the real migration.
# No default: an unset value skips these tests rather than pointing at
# whichever directory happened to exist on the author's machine.
V2_ROOT = os.environ.get("HORILLA_V2_ROOT", "")

pytestmark = pytest.mark.skipif(
    not (V2_ROOT and Path(HR_DUMP).exists() and Path(V2_ROOT, "manage.py").exists()),
    reason="set HORILLA_V2_ROOT to a v2 checkout, and build the HR fixture",
)


def _run_full_migration(db, source_dump):
    """Restore a v1 database and migrate it to v2, as the tool will.

    Mirrors the three steps the migration entrypoint has to perform:
      1. unapply ledger rows whose NAMES collide with v2 migration files
         (v1 and v2 both auto-generated e.g. base.0001_initial)
      2. record horilla_auth.0001_initial ahead of admin.0001_initial
      3. migrate with schema adoption enabled
    """
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(
        ["pg_restore", "-d", db, "--no-owner", "--no-privileges", source_dump],
        capture_output=True, check=False,
    )

    applied = set(psql(db, "select app||'/'||name from django_migrations").split())
    shipped = {
        f"{p.parts[-3]}/{p.stem}"
        for p in Path(V2_ROOT).glob("*/migrations/0*.py")
    }
    for name in sorted(applied & shipped):
        app, migration = name.split("/")
        subprocess.run(
            ["psql", "-q", "-d", db, "-c",
             f"delete from django_migrations where app='{app}' and name='{migration}'"],
            check=True,
        )

    env = {**os.environ, "HORILLA_ADOPT_EXISTING_SCHEMA": "1"}
    python = str(Path(V2_ROOT, ".venv/bin/python"))

    # Point the checkout at this database for the duration of the run.
    env_file = Path(V2_ROOT, ".env")
    original = env_file.read_text()
    env_file.write_text(
        "\n".join(
            f"DATABASE_URL=postgres://{os.environ.get('USER')}@localhost:5432/{db}"
            if line.startswith("DATABASE_URL=") else line
            for line in original.splitlines()
        )
    )
    try:
        subprocess.run(
            [python, "-c",
             "import django,os,sys;sys.path.insert(0,'.');"
             "os.environ.setdefault('DJANGO_SETTINGS_MODULE','horilla.settings');"
             "django.setup();"
             "from django.db import connection;"
             "from horilla.migration.adopt import backdate_auth_migration;"
             "backdate_auth_migration(connection)"],
            cwd=V2_ROOT, env=env, capture_output=True, check=True,
        )
        result = subprocess.run(
            [python, "manage.py", "migrate", "--noinput"],
            cwd=V2_ROOT, env=env, capture_output=True, text=True,
        )
    finally:
        env_file.write_text(original)

    assert result.returncode == 0, f"migrate failed:\n{result.stdout[-3000:]}"


@pytest.fixture(scope="module")
def migrated_db():
    """Module-scoped: a full 185-migration run takes ~90s, so it happens once
    and every assertion reads the same result. Nothing here mutates the
    database, so sharing it is safe."""
    db = "leavecopy_test"
    _run_full_migration(db, HR_DUMP)
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


def test_holiday_survives_the_app_move(migrated_db):
    """The regression this migration exists for."""
    assert psql(migrated_db, "select count(*) from base_holidays") == "1"


def test_holiday_fields_are_intact(migrated_db):
    """Not just the row count -- the dates and recurrence must be unchanged.
    A holiday that survives with the wrong dates is still a data bug."""
    row = psql(
        migrated_db,
        "select name||'|'||start_date||'|'||end_date||'|'||recurring "
        "from base_holidays",
    )
    assert row == "Onam|2024-09-15|2024-09-17|true"


def test_new_v2_field_takes_its_model_default(migrated_db):
    """is_specific is NOT NULL with no database default, so the copy has to
    supply it. False means 'applies to everyone', which is the right reading
    of a v1 holiday that had no such concept."""
    assert psql(migrated_db, "select is_specific from base_holidays") == "f"


def test_company_leave_survives(migrated_db):
    assert psql(migrated_db, "select count(*) from base_companyleaves") == "1"
    assert psql(
        migrated_db,
        "select coalesce(based_on_week,'-')||'|'||based_on_week_day "
        "from base_companyleaves",
    ) == "1|6"


def test_source_tables_are_gone_afterwards(migrated_db):
    """leave/0005 still runs; the point is that the copy happens first."""
    for table in ("leave_holiday", "leave_companyleave"):
        assert psql(
            migrated_db,
            f"select count(*) from information_schema.tables "
            f"where table_name='{table}'",
        ) == "0"


def test_null_company_does_not_become_an_m2m_row(migrated_db):
    """The HR fixture leaves company_id NULL. A row with no company in v1 must
    not be attributed to one in v2 -- that would be inventing tenant
    ownership."""
    assert psql(
        migrated_db, "select count(*) from base_companyleaves_company_id"
    ) == "0"


def test_full_migration_preserves_hr_data(migrated_db):
    """Guards the wider migration, not just the leave copy: row counts and
    money must come through untouched."""
    assert psql(migrated_db, "select count(*) from base_company") == "2"
    assert psql(migrated_db, "select count(*) from employee_employee") == "6"
    assert psql(migrated_db, "select count(*) from attendance_attendance") == "15"
    assert psql(migrated_db, "select count(*) from payroll_payslip") == "3"
    # Exact to the cent -- a re-typed money column shows up here.
    assert psql(migrated_db, "select sum(net_pay) from payroll_payslip") == "133752.93"
    assert psql(migrated_db, "select sum(gross_pay) from payroll_payslip") == "144600.99"


def test_company_attribution_is_preserved(migrated_db):
    """Multi-company scoping: the 3/2 split must survive, and the employee
    deliberately created without a company must stay without one."""
    # splitlines, not split: company names contain spaces.
    split = psql(migrated_db, """
        select c.company||'='||count(*)
        from employee_employeeworkinformation wi
        join base_company c on c.id = wi.company_id_id
        group by c.company order by 1
    """).splitlines()
    assert split == ["Acme Manufacturing=3", "Globex Services=2"]
    assert psql(
        migrated_db,
        "select count(*) from employee_employeeworkinformation "
        "where company_id_id is null",
    ) == "1"


# --- the three apps that had zero rows until the fixture was extended -----

def test_leave_balances_and_requests_survive(migrated_db):
    """AvailableLeave/LeaveRequest carry float day counts; a re-typed column
    would show up as a changed sum."""
    assert psql(migrated_db, "select count(*) from leave_availableleave") == "3"
    assert psql(migrated_db, "select count(*) from leave_leaverequest") == "3"
    assert psql(migrated_db, "select sum(available_days) from leave_availableleave") == "25.5"
    assert psql(migrated_db, "select sum(carryforward_days) from leave_availableleave") == "7.5"


def test_leave_request_spanning_a_month_end_is_unchanged(migrated_db):
    """28 Feb -> 3 Mar 2024 crosses both a month end and a leap day. Any
    date-arithmetic assumption in the migration path breaks here first."""
    assert psql(
        migrated_db,
        "select distinct start_date||'|'||end_date||'|'||requested_days||'|'||status "
        "from leave_leaverequest",
    ) == "2024-02-28|2024-03-03|5|approved"


def test_recruitment_pipeline_survives(migrated_db):
    assert psql(migrated_db, "select count(*) from recruitment_recruitment") == "1"
    assert psql(migrated_db, "select count(*) from recruitment_stage") == "4"
    assert psql(migrated_db, "select count(*) from recruitment_candidate") == "3"


def test_recruitment_stage_types_are_not_remapped(migrated_db):
    """A choice value with no v2 equivalent must not silently become NULL or
    collapse to the first choice."""
    assert psql(
        migrated_db,
        "select string_agg(distinct stage_type, ',') from recruitment_stage",
    ) == "applied,hired,initial,interview"


def test_onboarding_records_survive(migrated_db):
    assert psql(migrated_db, "select count(*) from onboarding_onboardingstage") == "4"
    assert psql(migrated_db, "select count(*) from onboarding_candidatestage") == "1"


def test_attendance_times_do_not_shift(migrated_db):
    """Clock-in is stored as a time and the dates span a month end. A naive
    timezone re-interpretation moves the instant."""
    assert psql(
        migrated_db,
        "select min(attendance_date)||'|'||max(attendance_date)||'|'||min(attendance_clock_in) "
        "from attendance_attendance",
    ) == "2024-03-29|2024-04-02|09:15:00"
