"""The leave->base copy must not skip rows because the target reuses their ids.

`base_holidays` is empty when the copy runs *if* the v1 database never had that
table -- which is what every other test for this copy assumes, because the HR
fixture is such a database. From some 1.x version onward Horilla already stores
holidays in `base`, so the target arrives at the copy holding unrelated rows at
ids 1..n.

The copy used to de-duplicate on `id`:

    where not exists (select 1 from base_holidays t where t.id = leave_holiday.id)

Against a populated target every source id collides, so every source row is
discarded as "already present". The run reports 0 carried, prints nothing, and
the migration reports success -- silent loss of the data this copy exists to
protect. Measured on a reconstruction of a real customer database at their exact
commit: 3 rows in, 0 copied, no warning.

These tests execute the shipped `_COPY_SCRIPT` itself rather than a
reimplementation of its SQL, so they cannot drift from what actually runs.
"""

import re
import pathlib
import sys
import types

import pytest

from horillasetup import migrate_v1


def _render_copy_script():
    """The real script, with the real column lists and natural keys."""
    return migrate_v1._COPY_SCRIPT.format(
        holiday=migrate_v1._HOLIDAY_COLUMNS,
        company_leave=migrate_v1._COMPANY_LEAVE_COLUMNS,
        holiday_key=migrate_v1._NATURAL_KEYS["base_holidays"],
        company_leave_key=migrate_v1._NATURAL_KEYS["base_companyleaves"],
    )


def _run_copy(conn):
    """Execute the shipped script against `conn`, standing in for django.db."""
    cursor = conn.cursor()

    class _Cursor:
        def __enter__(self):
            return cursor

        def __exit__(self, *exc):
            return False

    shim = types.ModuleType("django.db")
    shim.connection = types.SimpleNamespace(cursor=lambda: _Cursor())
    django_pkg = types.ModuleType("django")
    django_pkg.db = shim

    saved = {k: sys.modules.get(k) for k in ("django", "django.db")}
    sys.modules["django"] = django_pkg
    sys.modules["django.db"] = shim
    try:
        exec(compile(_render_copy_script(), "<copy>", "exec"), {})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


HOLIDAY_DDL = """
create table {name} (
    id serial primary key,
    name varchar(200) not null,
    start_date date not null,
    end_date date,
    recurring boolean not null default false,
    is_active boolean not null default true,
    created_at timestamptz default now(),
    created_by_id bigint,
    modified_by_id bigint,
    company_id_id bigint
)
"""

COMPANY_LEAVE_DDL = """
create table {name} (
    id serial primary key,
    based_on_week varchar(100),
    based_on_week_day varchar(100) not null,
    is_active boolean not null default true,
    created_at timestamptz default now(),
    created_by_id bigint,
    modified_by_id bigint,
    company_id_id bigint
)
"""


@pytest.fixture
def leave_tables(pg_connection):
    """A v1 database where `base` ALREADY holds holidays -- the customer's shape."""
    with pg_connection.cursor() as cur:
        for name in ("leave_holiday", "base_holidays"):
            cur.execute(HOLIDAY_DDL.format(name=name))
        for name in ("leave_companyleave", "base_companyleaves"):
            cur.execute(COMPANY_LEAVE_DDL.format(name=name))

        # The target already holds unrelated rows, taking ids 1..3.
        cur.execute(
            "insert into base_holidays (name, start_date) values "
            "('New Year''s Day','2025-01-01'),"
            "('Republic Day','2025-01-26'),"
            "('Christmas','2025-01-25')"
        )
        # The source holds different holidays -- which will also be ids 1..3.
        cur.execute(
            "insert into leave_holiday (name, start_date) values "
            "('Onam','2024-09-15'),"
            "('Diwali','2026-11-08'),"
            "('Company Day','2026-06-15')"
        )
    return pg_connection


def _names(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"select name from {table} order by name")
        return [r[0] for r in cur.fetchall()]


def test_source_rows_are_copied_even_though_their_ids_are_taken(leave_tables):
    """The regression. Every source id collides with an existing target id."""
    conn = leave_tables
    with conn.cursor() as cur:
        cur.execute("select id from leave_holiday order by id")
        source_ids = [r[0] for r in cur.fetchall()]
        cur.execute("select id from base_holidays order by id")
        target_ids = [r[0] for r in cur.fetchall()]
    assert set(source_ids) & set(target_ids), (
        "this test is pointless unless the ids actually collide"
    )

    _run_copy(conn)

    names = _names(conn, "base_holidays")
    assert len(names) == 6, f"expected 3 carried across, got {names}"
    for carried in ("Onam", "Diwali", "Company Day"):
        assert carried in names, f"{carried} was dropped"


def test_a_row_already_present_by_natural_key_is_not_duplicated(leave_tables):
    """Idempotency still holds -- on content now, rather than on id."""
    conn = leave_tables
    with conn.cursor() as cur:
        cur.execute(
            "insert into leave_holiday (name, start_date) "
            "values ('New Year''s Day','2025-01-01')"
        )

    _run_copy(conn)

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from base_holidays "
            "where name = 'New Year''s Day' and start_date = '2025-01-01'"
        )
        assert cur.fetchone()[0] == 1, "the duplicate was copied in anyway"


def test_running_the_copy_twice_carries_nothing_the_second_time(leave_tables):
    conn = leave_tables
    _run_copy(conn)
    first = _names(conn, "base_holidays")
    _run_copy(conn)
    assert _names(conn, "base_holidays") == first, "second run duplicated rows"


def test_nullable_key_columns_still_match(leave_tables):
    """
    based_on_week is nullable. Matching with `=` would make NULL never equal
    NULL, so a re-run would insert the row again -- and v2 puts a
    unique_together on exactly this pair, so the second insert would fail the
    constraint the migration is about to add.
    """
    conn = leave_tables
    with conn.cursor() as cur:
        cur.execute(
            "insert into leave_companyleave (based_on_week, based_on_week_day) "
            "values (null, '6')"
        )

    _run_copy(conn)
    _run_copy(conn)

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from base_companyleaves "
            "where based_on_week is null and based_on_week_day = '6'"
        )
        assert cur.fetchone()[0] == 1, "nullable key matched by = instead of IS NOT DISTINCT FROM"


def test_the_shipped_script_is_what_was_tested():
    """Guard against this file drifting into testing its own copy of the SQL."""
    rendered = _render_copy_script()
    assert "is not distinct from" in rendered
    assert "t.id = " not in rendered, "the id-based match is back"
    assert "'id'" not in str(migrate_v1._HOLIDAY_COLUMNS), "id is being copied again"
