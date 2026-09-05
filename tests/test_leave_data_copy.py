"""Holiday and CompanyLeave must survive v2's move from `leave` to `base`.

v2 creates base_holidays and deletes leave_holiday with no data step between
them. Without the copy in migrate_v1, every holiday and company-leave rule a
customer configured is silently destroyed -- confirmed against a real 1.6.1
database before the fix: 1 row in, 0 out, with `migrate` reporting success.

The window for the copy is narrower than it looks. Measured plan positions:

    16  base/0001   creates base_holidays
    43  leave/0005  DESTROYS leave_holiday
    84  base/0012   converts companyleaves.company_id from FK to M2M

There is no point where the source still exists and the destination has its
final shape, so the copy runs in the 16-43 window and lets base/0012 convert
what it wrote. test_the_copy_window_still_exists pins that ordering: if a
future v2 reorders these, the copy silently stops working, and this is the
test that says so.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import database_url, dump_path, psql

TOOL_ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = Path(os.environ.get("HORILLA_V2_ROOT", ""))
CLI = TOOL_ROOT / ".venv/bin/horillasetup"
HR_DUMP = os.environ.get(
    "HORILLA_V1_HR_DUMP", dump_path("1.6.1").replace(".dump", "_full.dump")
)

pytestmark = pytest.mark.skipif(
    not (str(V2_ROOT) and (V2_ROOT / "manage.py").exists()
         and CLI.exists() and Path(HR_DUMP).exists()),
    reason="set HORILLA_V2_ROOT to a v2 checkout and build the HR fixture",
)


def _with_db(db, fn):
    """Point the checkout at `db` for the duration of `fn`."""
    env_file = V2_ROOT / ".env"
    original = env_file.read_text()
    env_file.write_text("\n".join(
        f"DATABASE_URL={database_url(db)}"
        if line.startswith("DATABASE_URL=") else line
        for line in original.splitlines()
    ))
    try:
        return fn()
    finally:
        env_file.write_text(original)


@pytest.fixture(scope="module")
def migrated():
    """One full migration of the HR fixture. Takes ~3 minutes, so shared."""
    db = "leave_copy"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(["pg_restore", "-d", db, "--no-owner", "--no-privileges",
                    HR_DUMP], capture_output=True, check=False)

    before = {
        "holidays": psql(db, "select count(*) from leave_holiday"),
        "company_leaves": psql(db, "select count(*) from leave_companyleave"),
    }
    assert before["holidays"] != "0", "fixture has no holiday to lose"

    result = _with_db(db, lambda: subprocess.run(
        [str(CLI), "migrate", "hrms-v2", "--from-v1", "--yes", "--skip-backup"],
        cwd=V2_ROOT, capture_output=True, text=True,
    ))
    assert result.returncode == 0, result.stdout[-3000:]
    yield {"db": db, "before": before, "output": result.stdout}
    subprocess.run(["dropdb", "--if-exists", db], check=True)


def test_the_copy_window_still_exists(migrated):
    """The copy depends on leave/0005 running after base/0001 and before
    base/0012. Django does not order base/0012 and leave/0005 relative to each
    other, so this is a property of the plan, not a guarantee -- pinned here
    because if it changes, the copy stops working silently."""
    script = V2_ROOT / "_pytest_order.py"
    script.write_text(
        "import django; django.setup()\n"
        "from django.db.migrations.loader import MigrationLoader\n"
        "from django.db import connection\n"
        "g = MigrationLoader(connection, ignore_no_migrations=True).graph\n"
        "seen, order = set(), []\n"
        "for leaf in g.leaf_nodes():\n"
        "    for node in g.forwards_plan(leaf):\n"
        "        if node not in seen:\n"
        "            seen.add(node); order.append(node)\n"
        "idx = {n: i for i, n in enumerate(order)}\n"
        "for app, prefix in [('base','0001'), ('leave','0005'), ('base','0012')]:\n"
        "    match = [k for k in idx if k[0] == app and k[1].startswith(prefix)]\n"
        "    print('::POS', app, prefix, idx[match[0]] if match else -1)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(TOOL_ROOT),
           "DJANGO_SETTINGS_MODULE": "horillasetup.migration_settings"}
    try:
        out = _with_db(migrated["db"], lambda: subprocess.run(
            [str(V2_ROOT / ".venv/bin/python"), script.name],
            cwd=V2_ROOT, env=env, capture_output=True, text=True,
        ))
    finally:
        script.unlink(missing_ok=True)

    positions = {f"{p[1]}/{p[2]}": int(p[3])
                 for p in (l.split() for l in out.stdout.splitlines())
                 if p and p[0] == "::POS"}
    assert positions["base/0001"] < positions["leave/0005"], (
        "base_holidays is no longer created before leave_holiday is destroyed"
    )
    assert positions["leave/0005"] < positions["base/0012"], (
        "leave/0005 no longer runs before base/0012 -- the copy window moved"
    )


def test_holidays_survive(migrated):
    """The regression this whole step exists for."""
    assert psql(migrated["db"], "select count(*) from base_holidays") \
        == migrated["before"]["holidays"]


def test_holiday_fields_are_intact(migrated):
    """Not just the count -- a holiday that survives with the wrong dates is
    still a data bug."""
    assert psql(migrated["db"],
                "select name||'|'||start_date||'|'||end_date||'|'||recurring "
                "from base_holidays") == "Onam|2024-09-15|2024-09-17|true"


def test_new_v2_field_takes_its_model_default(migrated):
    """is_specific does not exist in v1 and arrives later in base/0006, so it
    takes the model default. False means 'applies to everyone', the right
    reading of a v1 holiday that had no such concept."""
    assert psql(migrated["db"], "select is_specific from base_holidays") == "f"


def test_company_leave_survives_with_its_values(migrated):
    assert psql(migrated["db"], "select count(*) from base_companyleaves") \
        == migrated["before"]["company_leaves"]
    assert psql(migrated["db"],
                "select coalesce(based_on_week,'-')||'|'||based_on_week_day "
                "from base_companyleaves") == "1|6"


def test_the_source_tables_are_still_removed(migrated):
    """leave/0005 must still run: the copy happens first, it does not replace
    the migration."""
    assert psql(migrated["db"],
                "select count(*) from information_schema.tables "
                "where table_name in ('leave_holiday','leave_companyleave')") == "0"


def test_null_company_does_not_become_an_m2m_row(migrated):
    """base/0012 converts company_id from FK to M2M after the copy. A row with
    no company in v1 must not gain one -- that would be inventing tenant
    ownership."""
    assert psql(migrated["db"],
                "select count(*) from base_companyleaves_company_id") == "0"


def test_the_run_reports_what_it_carried(migrated):
    """Silent success is what made this bug survive. The operator should see
    the rows move."""
    assert "carried 2 holiday/company-leave row(s)" in migrated["output"]


def test_verification_would_catch_the_loss(migrated):
    """Stage 6 counts these before and after, so a future regression fails the
    run rather than reporting success over an empty table."""
    assert "Stage 6/6" in migrated["output"]
    assert "❌" not in migrated["output"]


# --- the wider migration, verified on the same run ------------------------

def test_hr_data_survives(migrated):
    db = migrated["db"]
    assert psql(db, "select count(*) from base_company") == "2"
    assert psql(db, "select count(*) from employee_employee") == "6"
    assert psql(db, "select count(*) from attendance_attendance") == "15"
    assert psql(db, "select count(*) from payroll_payslip") == "3"


def test_money_is_exact_to_the_cent(migrated):
    """v1 stores money as double precision, so a re-typed column shows here."""
    db = migrated["db"]
    assert psql(db, "select sum(net_pay) from payroll_payslip") == "133752.93"
    assert psql(db, "select sum(gross_pay) from payroll_payslip") == "144600.99"


def test_company_attribution_is_preserved(migrated):
    """Multi-company scoping: the 3/2 split must survive, and the employee
    deliberately created without a company must stay without one."""
    split = psql(migrated["db"], """
        select c.company||'='||count(*)
        from employee_employeeworkinformation wi
        join base_company c on c.id = wi.company_id_id
        group by c.company order by 1
    """).splitlines()
    assert split == ["Acme Manufacturing=3", "Globex Services=2"]
    assert psql(migrated["db"],
                "select count(*) from employee_employeeworkinformation "
                "where company_id_id is null") == "1"


def test_leave_balances_and_requests_survive(migrated):
    db = migrated["db"]
    assert psql(db, "select count(*) from leave_availableleave") == "3"
    assert psql(db, "select sum(available_days) from leave_availableleave") == "25.5"
    assert psql(db, "select sum(carryforward_days) from leave_availableleave") == "7.5"


def test_leave_request_spanning_a_month_end_is_unchanged(migrated):
    """28 Feb -> 3 Mar 2024 crosses a month end and a leap day. Any date
    assumption in the migration path breaks here first."""
    assert psql(migrated["db"],
                "select distinct start_date||'|'||end_date||'|'||requested_days"
                "||'|'||status from leave_leaverequest") \
        == "2024-02-28|2024-03-03|5|approved"


def test_recruitment_and_onboarding_survive(migrated):
    db = migrated["db"]
    assert psql(db, "select count(*) from recruitment_candidate") == "3"
    assert psql(db, "select count(*) from onboarding_onboardingstage") == "4"
    # A choice value with no v2 equivalent must not collapse to the first choice.
    assert psql(db, "select string_agg(distinct stage_type, ',') "
                    "from recruitment_stage") == "applied,hired,initial,interview"


def test_attendance_times_do_not_shift(migrated):
    """Clock-in is a time and the dates span a month end; a naive timezone
    re-interpretation moves the instant."""
    assert psql(migrated["db"],
                "select min(attendance_date)||'|'||max(attendance_date)||'|'"
                "||min(attendance_clock_in) from attendance_attendance") \
        == "2024-03-29|2024-04-02|09:15:00"
