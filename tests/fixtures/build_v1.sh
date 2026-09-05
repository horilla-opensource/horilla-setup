#!/usr/bin/env bash
#
# Build a real Horilla v1 database for a given tag, for migration testing.
#
#   ./build_v1.sh 1.6.1 [dbname]
#
# Why this exists: no v1 tag ships committed migrations (.gitignore excludes
# **/migrations/**), so a v1 database's schema is whatever `makemigrations`
# generated at install time. There is no way to obtain a v1 schema except to
# reproduce that install. Everything downstream depends on this being
# reproducible rather than a hand-built one-off.
#
# The sequence below is not the obvious one. `migrate` alone creates only 13
# tables (Django's own apps) and exits 0 -- none of Horilla's ~28 apps, because
# they have no migration files. `makemigrations` must run first, and a fresh
# database needs `migrate` before that to have a migration table at all.
#
set -euo pipefail

TAG="${1:?usage: build_v1.sh <tag> [dbname]}"
DB="${2:-horilla_v1_${TAG//./_}}"
WORK="${HORILLA_V1_WORKDIR:-/tmp/horilla-v1-fixtures}"
SRC="$WORK/src-$TAG"
REPO="${HORILLA_V1_REPO:-https://github.com/horilla-opensource/horilla.git}"

# Resolved before any cd: this script cds into the v1 checkout later, so a
# relative path to seed_v1.py would break there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUPPORTED=(1.3.2 1.4.0 1.5.0 1.6.0 1.6.1)
if [[ ! " ${SUPPORTED[*]} " =~ " ${TAG} " ]]; then
  echo "error: $TAG is not a supported migration source." >&2
  echo "       supported: ${SUPPORTED[*]}  (1.2.x is deliberately out of scope)" >&2
  exit 2
fi

command -v psql    >/dev/null || { echo "error: psql not found" >&2; exit 1; }
command -v createdb >/dev/null || { echo "error: createdb not found" >&2; exit 1; }
pg_isready -q     || { echo "error: no Postgres server reachable" >&2; exit 1; }

echo "==> v1 fixture: tag=$TAG db=$DB"

mkdir -p "$WORK"
if [ ! -d "$SRC/.git" ]; then
  echo "--> cloning $TAG"
  git clone -q --depth 1 -b "$TAG" "$REPO" "$SRC"
else
  echo "--> reusing $SRC"
fi

if [ ! -d "$SRC/.venv" ]; then
  echo "--> creating venv (v1 pins Django 4.2; v2 uses 5.2, so this must be separate)"
  python3 -m venv "$SRC/.venv"
  "$SRC/.venv/bin/pip" install -q --upgrade pip

  # Tag 1.4.0 ships a corrupt requirements.txt: UTF-16LE with a BOM (every
  # other tag is ASCII) AND an odd byte count -- 2047 bytes, so the final
  # codepoint is truncated. pip dies with UnicodeDecodeError, and iconv exits
  # 0 while silently dropping the tail. Python's errors="ignore" is the only
  # thing that reads it cleanly. Normalised rather than skipped: a customer
  # sitting on 1.4.0 is still a supported migration source.
  REQ="$SRC/requirements.txt"
  if file "$REQ" | grep -qi 'UTF-16'; then
    echo "    normalising corrupt UTF-16 requirements.txt (1.4.0 packaging defect)"
    python3 -c "
import sys
raw = open(sys.argv[1], 'rb').read()
text = raw.decode('utf-16', errors='ignore')
open(sys.argv[1], 'w', encoding='utf-8').write(text)
" "$REQ"
  fi

  "$SRC/.venv/bin/pip" install -q -r "$REQ"
fi

echo "--> recreating database $DB"
dropdb --if-exists "$DB"
createdb "$DB"

# DEBUG=False deliberately: migration behaviour under DEBUG=True is not what
# customers run, and Django changes some defaults between the two.
cat > "$SRC/.env" <<EOF
DEBUG=False
SECRET_KEY=fixture-only-not-a-real-secret-$(date +%s)-abcdefghijklmnop
DB_ENGINE=django.db.backends.postgresql
DB_NAME=$DB
DB_USER=${PGUSER:-$(whoami)}
DB_PASSWORD=${PGPASSWORD:-}
DB_HOST=${PGHOST:-localhost}
DB_PORT=${PGPORT:-5432}
ALLOWED_HOSTS=localhost,127.0.0.1
EOF

cd "$SRC"
PY="$SRC/.venv/bin/python"

# Order matters -- see the header comment.
echo "--> migrate (Django's own apps; creates django_migrations)"
"$PY" manage.py migrate --noinput >/dev/null
echo "--> makemigrations (generates Horilla's ~28 apps; v1 ships none)"
"$PY" manage.py makemigrations >/dev/null
echo "--> migrate (applies them)"
"$PY" manage.py migrate --noinput >/dev/null

TABLES=$(psql -d "$DB" -tAc \
  "select count(*) from information_schema.tables where table_schema='public';")
if [ "$TABLES" -lt 300 ]; then
  echo "error: only $TABLES tables created; expected ~341." >&2
  echo "       'migrate' alone yields 13 -- makemigrations likely failed." >&2
  exit 1
fi
echo "--> $TABLES tables"

echo "--> seeding users, groups and permissions"
"$PY" manage.py shell < "$HERE/seed_v1.py"

# HR data is opt-in: the user-only fixture is enough for auth-level tests and
# builds in seconds, while this adds companies, employees, attendance,
# payroll, leave, recruitment and onboarding for the data-migration tests.
# Scale fixture: raw-SQL bulk seed for measuring the migration under load.
# Sized by HORILLA_SCALE_EMPLOYEES / _ATT_DAYS / _PAYSLIPS.
if [ -n "${HORILLA_SCALE_EMPLOYEES:-}" ]; then
  echo "--> seeding scale data (${HORILLA_SCALE_EMPLOYEES} employees)"
  "$PY" manage.py shell < "$HERE/seed_v1_scale.py"
  DUMP_SUFFIX_EXTRA="_scale${HORILLA_SCALE_EMPLOYEES}"
fi

if [ "${HORILLA_SEED_HR_DATA:-0}" = "1" ]; then
  echo "--> seeding HR data (companies, employees, payroll, leave, recruitment)"
  "$PY" manage.py shell < "$HERE/seed_v1_hr_data.py"
  DUMP_SUFFIX_EXTRA="_full"
fi

DUMP="$WORK/v1_$TAG${DUMP_SUFFIX_EXTRA:-}.dump"
pg_dump -Fc "$DB" -f "$DUMP"
echo "==> done: db=$DB dump=$DUMP ($(du -h "$DUMP" | cut -f1))"
