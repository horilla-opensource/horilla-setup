# Changelog

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
