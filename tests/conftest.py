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


def dump_path(tag, variant=""):
    """Where build_v1.sh writes a fixture.

    The suffix is not cosmetic: seeding HR data makes the builder write
    v1_<tag>_full.dump instead of v1_<tag>.dump. A caller that wants either
    should use find_dump().
    """
    return os.path.join(WORKDIR, f"v1_{tag}{variant}.dump")


def find_dump(tag, exact=False):
    """A fixture for `tag`, or None.

    The two variants are NOT interchangeable, which is the whole reason this
    takes a flag. The plain dump has 10 users and 5 null last_logins; the
    HR-seeded one has 16 and 11, plus companies, employees and payroll. A test
    characterising the plain fixture gets wrong answers from the seeded one
    rather than a clean skip -- which is exactly what happened when this
    silently fell back: ten failures asserting `16 == 10` against a CI cache
    that happened to hold only _full dumps.

    exact=True returns only the plain dump, so a test that depends on its
    contents skips instead of measuring the wrong database. The default is
    lenient, for tests that only need *a* v1 schema and do not care which.
    """
    if exact:
        plain = dump_path(tag)
        return plain if os.path.exists(plain) else None
    for variant in ("", "_full"):
        candidate = dump_path(tag, variant)
        if os.path.exists(candidate):
            return candidate
    return None


def restore_dump(db, path):
    """Restore `path` into `db`, and fail loudly if nothing arrived.

    pg_restore exits non-zero on ownership and ACL warnings that are harmless
    here, so its exit code cannot be trusted -- but it was previously ignored
    entirely with check=False, which meant a restore that produced NOTHING was
    indistinguishable from one that worked. Tests then ran against an empty
    database and failed much later with a baffling "relation does not exist".

    So the result is checked rather than the exit code.
    """
    subprocess.run(
        ["pg_restore", "-d", db, "--no-owner", "--no-privileges", path],
        capture_output=True, check=False,
    )
    if table_count(db) == 0:
        raise AssertionError(
            f"pg_restore produced no tables in {db} from {path}. "
            "The dump is missing or unreadable."
        )


def database_url(db):
    """A DATABASE_URL for `db` honouring libpq's own environment variables.

    The tests shell out to psql/pg_restore, which read PGHOST/PGUSER/PGPASSWORD
    themselves, but Django needs the same connection spelled out. Hardcoding
    $USER@localhost with no password works on a developer's machine and fails
    everywhere else -- notably CI, where the OS user is `runner` and the
    Postgres role has a password.
    """
    user = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    credentials = f"{user}:{password}" if password else user
    return f"postgres://{credentials}@{host}:{port}/{db}"


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
    if find_dump(tag) is None:
        pytest.skip(f"no fixture for {tag}; run fixtures/build_v1.sh {tag}")
    return tag


@pytest.fixture
def migration_db(migration_tag):
    """Disposable restore for a full-migration test."""
    db = f"mig_e2e_{migration_tag.replace('.', '_')}"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    restore_dump(db, find_dump(migration_tag))
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


@pytest.fixture(params=SUPPORTED_TAGS)
def v1_tag(request):
    """Every supported source version, one test run each.

    Parameterised rather than looped so a failure names the tag it failed on.
    """
    tag = request.param
    # exact: test_v1_fixtures asserts this fixture's exact row counts, so the
    # HR-seeded superset is not a substitute.
    if find_dump(tag, exact=True) is None:
        pytest.skip(f"no plain fixture for {tag}; run fixtures/build_v1.sh {tag}")
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
    restore_dump(db, find_dump(v1_tag, exact=True))
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)
