"""A sequence sitting below max(id) must be repaired before migrate runs.

A real customer migration aborted part-way through stage 5 with

    psycopg2.errors.UniqueViolation: duplicate key value violates unique
    constraint "django_content_type_pkey"
    DETAIL:  Key (id)=(219) already exists.

Their `django_content_type` sequence was behind the highest id the table held.
Nothing notices such a sequence until something inserts without naming an id --
and `migrate`'s post_migrate signal does exactly that, via
`create_contenttypes` and `create_permissions`, for every model v2 adds.

The condition predates the migration. It is what a database looks like after
rows have been inserted with explicit primary keys at some point: a `loaddata`,
a copy between environments, a restore that did not reset sequences. The
migration is simply the first thing to insert enough rows to hit it.
"""

import psycopg2
import pytest

from horillasetup.migration.adopt import resync_sequences


def _table(cur, name="django_content_type"):
    cur.execute(f'drop table if exists "{name}" cascade')
    cur.execute(
        f'create table "{name}" ('
        "  id serial primary key,"
        "  app_label varchar(100) not null,"
        "  model varchar(100) not null)"
    )


def test_a_sequence_behind_its_table_is_moved_up(pg_connection):
    """The customer's exact shape: ids up to 219, sequence at 218."""
    with pg_connection.cursor() as cur:
        _table(cur)
        cur.execute(
            "insert into django_content_type (id, app_label, model) "
            "select g, 'app'||g, 'model'||g from generate_series(1, 219) g"
        )
        cur.execute("select setval('django_content_type_id_seq', 218, true)")

    repaired = resync_sequences(pg_connection)

    tables = [row[0] for row in repaired]
    assert "django_content_type" in tables
    entry = next(r for r in repaired if r[0] == "django_content_type")
    assert entry[2] == 218, "should report where the sequence was"
    assert entry[3] == 219, "should report where it moved to"

    # The insert that failed for the customer now succeeds.
    with pg_connection.cursor() as cur:
        cur.execute(
            "insert into django_content_type (app_label, model) "
            "values ('horilla', 'newmodel') returning id"
        )
        assert cur.fetchone()[0] == 220


def test_without_the_repair_the_insert_still_collides(pg_connection):
    """Guard the guard: the scenario must actually fail when unrepaired."""
    with pg_connection.cursor() as cur:
        _table(cur)
        cur.execute(
            "insert into django_content_type (id, app_label, model) "
            "select g, 'app'||g, 'model'||g from generate_series(1, 219) g"
        )
        cur.execute("select setval('django_content_type_id_seq', 218, true)")

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with pg_connection.cursor() as cur:
            cur.execute(
                "insert into django_content_type (app_label, model) "
                "values ('horilla', 'newmodel')"
            )
    pg_connection.rollback()


def test_a_healthy_sequence_is_left_alone(pg_connection):
    """Only sequences that are behind are touched, so the run is honest."""
    with pg_connection.cursor() as cur:
        _table(cur)
        cur.execute(
            "insert into django_content_type (app_label, model) "
            "values ('a', 'b'), ('c', 'd')"
        )

    repaired = resync_sequences(pg_connection)
    assert "django_content_type" not in [row[0] for row in repaired]


def test_an_empty_table_is_left_alone(pg_connection):
    """
    Forcing an empty table's sequence to 1 with is_called=true would skip id 1
    on the first insert -- a silent off-by-one introduced by the repair itself.
    """
    with pg_connection.cursor() as cur:
        _table(cur)

    repaired = resync_sequences(pg_connection)
    assert "django_content_type" not in [row[0] for row in repaired]

    with pg_connection.cursor() as cur:
        cur.execute(
            "insert into django_content_type (app_label, model) "
            "values ('a', 'b') returning id"
        )
        assert cur.fetchone()[0] == 1


def test_running_it_twice_changes_nothing_the_second_time(pg_connection):
    with pg_connection.cursor() as cur:
        _table(cur)
        cur.execute(
            "insert into django_content_type (id, app_label, model) "
            "values (5, 'a', 'b')"
        )
        cur.execute("select setval('django_content_type_id_seq', 1, true)")

    first = resync_sequences(pg_connection)
    second = resync_sequences(pg_connection)
    assert "django_content_type" in [row[0] for row in first]
    assert "django_content_type" not in [row[0] for row in second]
