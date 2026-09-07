# Changelog

## 1.1.1

### Fixed: the holiday copy silently dropped rows when the target already had data

The copy that carries Holiday and CompanyLeave from `leave` into `base` was
idempotent on `id`:

```sql
where not exists (select 1 from base_holidays t where t.id = leave_holiday.id)
```

That is correct only when `base_holidays` is empty or holds rows previously
copied from `leave_holiday`. On a v1 where holidays already live in `base` --
which is the case from some 1.x versions onward -- `base_holidays` holds
unrelated rows at ids 1..n, every source id collides, and every source row is
discarded as "already present". The run reports 0 carried, prints nothing, and
the migration reports success. Silent loss of exactly the data this copy exists
to protect.

Measured on a reconstruction of a real customer database: 3 rows in
`leave_holiday`, 0 copied, no warning.

The copy now matches on a natural key -- `(name, start_date)` for holidays and
`(based_on_week, based_on_week_day)` for company leaves, the latter being v2's
own `unique_together` -- and no longer copies `id`, letting the target assign
fresh ones. `is not distinct from` is used rather than `=` so that nullable
columns match instead of never matching.

Dropping `id` is safe: the v1 schema has no inbound foreign keys to
`leave_holiday` or `leave_companyleave`, and v2's M2M join tables are created by
the migration and empty when the copy runs. Both were verified rather than
assumed.

Every existing test for this copy started from an empty `base_holidays`, which
is why the defect shipped.

### Fixed: uniqueness v2 adds that v1 data violates aborted the migration at stage 5

A customer's migration died building `unique_work_record_per_employee_per_date`.
Horilla's own demo data contains 111 colliding `(employee, date)` pairs across
225 work records, so any v1 install that loaded the sample data hits it -- and
hits it part-way through stage 5, with a half-changed schema and a restore.

Pre-flight now checks every uniqueness rule v2 introduces -- 13
`unique_together` plus the `UniqueConstraint` set -- against the source database
before the backup is taken, and names the table, the columns and the number of
duplicates. Columns are resolved by asking the database whether the model field
is `field` or `field_id`, because Django's `_id` suffixing is not derivable from
the field list. NULLs are excluded, since Postgres treats them as distinct in a
unique index and reporting them would send the operator hunting a duplicate the
migration will accept.

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
