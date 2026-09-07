# Changelog

## 1.1.1

### Fixed: a migration could abort part-way on a database with a stale id sequence

A customer migration failed in stage 5 with

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
"django_content_type_pkey"
DETAIL:  Key (id)=(219) already exists.
```

Their `django_content_type` sequence was sitting below the highest id the table
held. Nothing notices such a sequence until something inserts without naming an
id, and `migrate`'s `post_migrate` signal does exactly that -- it runs
`create_contenttypes` and `create_permissions`, which insert a row per model v2
adds.

The stale sequence predates the migration: it is what a database looks like
after rows have been inserted with explicit primary keys, via a `loaddata`, a
copy between environments, or a restore that did not reset sequences. The
migration is simply the first thing to insert enough rows to hit it, so it took
the blame and the operator took a restore.

Stage 4 now moves every `id` sequence up to its table's true maximum before
`migrate` runs, and reports how many it reset. Empty tables and healthy
sequences are left alone. `setval` to `max(id)` is idempotent and can only move
a sequence forward to a value that was already correct.

Every table is checked rather than only `django_content_type` -- that is merely
the one `post_migrate` reaches first, and any table whose sequence is behind
would fail the same way the moment v2 inserts into it.


## 1.1.0

### Migrate a Horilla v1 database to v2

```bash
cd /path/to/your/horilla-v2
horillasetup migrate hrms-v2 --from-v1
```

Upgrades an existing v1 database (**1.3.2 – 1.6.1**) in place, keeping every user,
password, holiday and record. Six stages — fingerprint, pre-flight, backup,
ledger reconciliation, migrate, verify — where nothing before the backup writes to
the database, so a refusal leaves it exactly as it was.

Users keep their existing passwords: the stored hashes are carried across
untouched, not reset.

The Horilla codebase itself is not modified. The migration is served from this
tool via Django's `MIGRATION_MODULES`.

### Fixed: holidays were silently destroyed

v2 moves `Holiday` and `CompanyLeave` from the `leave` app to `base` with no data
step between creating the new tables and dropping the old ones. Upgrading without
this tool destroys every holiday and company-leave rule a customer configured,
while reporting success. Confirmed against a real 1.6.1 database: 1 row in, 0 out.

### Removed: `migrate hrms-v2 --existing`

It deleted every row in `django_migrations` and then faked the whole migration
graph, leaving the database recording itself as v2 while the schema was still v1 —
so tables v2 added were never created, with no backup and no way to notice. It now
exits with a pointer to `--from-v1` rather than running.

If you have `--existing` in a script, replace it with `--from-v1`.

### Testing

101 tests against real Postgres and real v1 databases built from real release
tags, run in CI on every push and weekly against Horilla v2's `dev/v2.0`. The
weekly run matters because v2 moves independently of this tool.

---

## 1.0.2 – 1.0.4

Published to PyPI in January and February 2026. The version bumps were not
committed, so this repository still recorded 1.0.1 until 1.1.0; the released code
was otherwise the same as 1.0.1.

## 1.0.0 – 1.0.1

Initial release: `build`, `migrate`, `upgrade` and `install-deps` for HRMS v1,
HRMS v2 and CRM.
