"""The v1 fixtures are the ground truth for every later migration phase.

If these drift, every migration assertion built on top is measuring the wrong
thing -- so the fixture shape is asserted explicitly rather than assumed.
"""

from conftest import (
    SCHEMA_VARIANTS,
    SUPPORTED_TAGS,
    column_set,
    row_count,
    table_count,
)

# Measured from five real fixture databases, not from reading model code.
EXPECTED_TABLES = 341
EXPECTED_COLUMNS = {"pre-1.5": 2853, "1.5-plus": 2858}

# Present only from 1.5.0 onward. Any migration that assumes these exist will
# fail on a 1.3.2 or 1.4.0 customer.
POST_15_COLUMNS = {
    "attendance_attendance.approved_by_id",
    "attendance_historicalattendance.approved_by_id",
    "horilla_backup_googledrivebackup.access_token",
    "horilla_backup_googledrivebackup.oauth_credentials_file",
    "horilla_backup_googledrivebackup.refresh_token",
    "horilla_backup_googledrivebackup.token_expiry",
}
PRE_15_ONLY = {"horilla_backup_googledrivebackup.service_account_file"}


def test_fixture_has_full_schema(v1_db, v1_tag):
    """`migrate` alone yields 13 tables and exits 0. A fixture that lost the
    makemigrations step would silently test almost nothing."""
    assert table_count(v1_db) == EXPECTED_TABLES, (
        f"{v1_tag}: expected {EXPECTED_TABLES} tables"
    )


def test_column_count_matches_its_variant(v1_db, v1_tag):
    variant = SCHEMA_VARIANTS[v1_tag]
    assert len(column_set(v1_db)) == EXPECTED_COLUMNS[variant], (
        f"{v1_tag} ({variant}) column count drifted"
    )


def test_version_conditionals_are_exactly_as_expected(v1_db, v1_tag):
    """The whole universal-tool design rests on there being exactly two
    schema variants. This is the test that proves it."""
    cols = column_set(v1_db)
    if SCHEMA_VARIANTS[v1_tag] == "1.5-plus":
        assert POST_15_COLUMNS <= cols, f"{v1_tag} missing 1.5+ columns"
        assert not (PRE_15_ONLY & cols), f"{v1_tag} still has pre-1.5 column"
    else:
        assert not (POST_15_COLUMNS & cols), f"{v1_tag} has 1.5+ columns early"
        assert PRE_15_ONLY <= cols, f"{v1_tag} missing service_account_file"


def test_seed_data_present(v1_db, v1_tag):
    """Migration assertions need users, groups and permissions to exist."""
    assert row_count(v1_db, "auth_user") == 10
    assert row_count(v1_db, "auth_group") == 2
    assert row_count(v1_db, "auth_user_groups") == 10
    assert row_count(v1_db, "auth_user_user_permissions") == 6


def test_seed_covers_the_edge_cases(v1_db):
    """Flags a migration must preserve: one inactive user, one superuser,
    and NULL last_login values."""
    from conftest import psql

    assert psql(v1_db, "select count(*) from auth_user where not is_active;") == "1"
    assert psql(v1_db, "select count(*) from auth_user where is_superuser;") == "1"
    assert psql(v1_db, "select count(*) from auth_user where last_login is null;") == "5"


def test_migration_ledger_is_populated(v1_db):
    """django_migrations is what the current tool deletes (finding F2).
    Its pre-migration state has to be observable for that to be provable."""
    assert row_count(v1_db, "django_migrations") > 50


def test_v1_has_no_v2_user_table(v1_db):
    """Guards against a fixture accidentally built from a migrated database."""
    from conftest import psql

    assert psql(
        v1_db,
        "select count(*) from information_schema.tables "
        "where table_name='horilla_auth_horillauser';",
    ) == "0"


def test_all_supported_tags_have_a_variant_mapping():
    assert set(SCHEMA_VARIANTS) == set(SUPPORTED_TAGS)
