"""Fixtures for v1 -> v2 migration tests.

These talk to a real Postgres via psycopg2 and psql/pg_restore -- deliberately
not Django's test client. The thing under test is a schema migration, so an
ORM-level fixture would abstract away exactly what can break.

Build the fixture databases first:

    for t in 1.3.2 1.4.0 1.5.0 1.6.0 1.6.1; do
        tests/fixtures/build_v1.sh "$t"
    done
"""

import os
import subprocess

import pytest

SUPPORTED_TAGS = ["1.3.2", "1.4.0", "1.5.0", "1.6.0", "1.6.1"]

# Two physical schema variants exist across the supported range, confirmed by
# column-level diff of five real fixture databases:
#   1.3.2 / 1.4.0  -> 2853 columns
#   1.5.0+         -> 2858 columns (adds attendance approved_by + GoogleDrive
#                     OAuth fields, drops service_account_file)
SCHEMA_VARIANTS = {
    "1.3.2": "pre-1.5",
    "1.4.0": "pre-1.5",
    "1.5.0": "1.5-plus",
    "1.6.0": "1.5-plus",
    "1.6.1": "1.5-plus",
}

WORKDIR = os.environ.get("HORILLA_V1_WORKDIR", "/tmp/horilla-v1-fixtures")


def dump_path(tag):
    return os.path.join(WORKDIR, f"v1_{tag}.dump")


def psql(db, sql):
    """One scalar value out of psql. Raises on a non-zero exit."""
    out = subprocess.run(
        ["psql", "-d", db, "-tAc", sql],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def table_count(db):
    return int(psql(db, "select count(*) from information_schema.tables "
                        "where table_schema='public';"))


def column_set(db):
    rows = psql(db, "select table_name||'.'||column_name "
                    "from information_schema.columns "
                    "where table_schema='public' order by 1;")
    return set(rows.splitlines())


def row_count(db, table):
    return int(psql(db, f"select count(*) from {table};"))


# Running a full migration for every tag in every test costs ~2 minutes each.
# The schema-level tests need all five; the end-to-end migration tests only
# need one per variant, which is what MIGRATION_TAGS gives them.
MIGRATION_TAGS = ["1.3.2", "1.6.1"]   # one per physical schema variant


@pytest.fixture(params=MIGRATION_TAGS)
def migration_tag(request):
    """One tag per schema variant, for tests that run a full migration."""
    tag = request.param
    if not os.path.exists(dump_path(tag)):
        pytest.skip(f"no fixture for {tag}; run fixtures/build_v1.sh {tag}")
    return tag


@pytest.fixture
def migration_db(migration_tag):
    """Disposable restore for a full-migration test."""
    db = f"mig_e2e_{migration_tag.replace('.', '_')}"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(
        ["pg_restore", "-d", db, "--no-owner", "--no-privileges",
         dump_path(migration_tag)],
        capture_output=True, check=False,
    )
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


@pytest.fixture(params=SUPPORTED_TAGS)
def v1_tag(request):
    """Every supported source version, one test run each.

    Parameterised rather than looped so a failure names the tag it failed on.
    """
    tag = request.param
    if not os.path.exists(dump_path(tag)):
        pytest.skip(f"no fixture for {tag}; run fixtures/build_v1.sh {tag}")
    return tag


@pytest.fixture
def v1_db(v1_tag):
    """A disposable restore of the v1 fixture for this tag.

    Restored fresh per test so a destructive migration cannot leak into the
    next one -- which matters here, because the tool under test deletes rows
    from django_migrations.
    """
    db = f"mig_test_{v1_tag.replace('.', '_')}"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(
        ["pg_restore", "-d", db, "--no-owner", "--no-privileges", dump_path(v1_tag)],
        capture_output=True, check=False,  # pg_restore warns on owner/ACL; not fatal
    )
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)
