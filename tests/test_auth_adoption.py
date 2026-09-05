"""Phase 3: horilla_auth adopts v1's auth_user table instead of replacing it.

Two starting points must both work from the same migration:

  fresh install -- auth_user does not exist, tables are created
  v1 upgrade    -- auth_user exists with rows and ~328 inbound foreign keys,
                   so the tables are adopted and nothing moves

These run the real migration through a real Django process against real
Postgres. A Django TestCase would not do: the thing under test is what happens
to a pre-existing physical schema, which a test-database fixture erases.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import dump_path, psql  # noqa: E402

# The models under test live in the HR checkout, which is a separate repo from
# this tool -- so it is located by environment variable, never by walking up
# from __file__.
V2_ROOT = Path(os.environ.get("HORILLA_V2_ROOT", ""))

# A v2 runtime is needed to exercise a v2 migration. Built by hand for now;
# CI will install it from requirements.txt.
# No default: an unset value skips these tests rather than pointing at
# whichever interpreter happened to exist on the author's machine.
V2_PYTHON = os.environ.get("HORILLA_V2_PYTHON", "")

pytestmark = pytest.mark.skipif(
    not (V2_PYTHON and Path(V2_PYTHON).exists()
         and str(V2_ROOT) and (V2_ROOT / "horilla_auth" / "models.py").exists()),
    reason="set HORILLA_V2_PYTHON and HORILLA_V2_ROOT to a v2 checkout",
)


@pytest.fixture
def harness(tmp_path):
    """A minimal Django project wired to the real horilla_auth models and
    migration, so the migration is exercised without booting all ~28 apps."""
    app = tmp_path / "horilla_auth"
    (app / "migrations").mkdir(parents=True)
    (app / "__init__.py").touch()
    (app / "migrations" / "__init__.py").touch()
    shutil.copy(V2_ROOT / "horilla_auth" / "models.py", app / "models.py")
    shutil.copy(
        V2_ROOT / "horilla_auth" / "migrations" / "0001_initial.py",
        app / "migrations" / "0001_initial.py",
    )

    (tmp_path / "settings.py").write_text(textwrap.dedent("""
        import os
        SECRET_KEY = "test-only-not-a-real-secret-0123456789abcdefghij"
        DEBUG = False
        ALLOWED_HOSTS = ["*"]
        INSTALLED_APPS = [
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "horilla_auth",
        ]
        AUTH_USER_MODEL = "horilla_auth.HorillaUser"
        DATABASES = {"default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["TEST_DB"],
            "USER": os.environ.get("USER"),
            "HOST": "localhost", "PORT": "5432",
        }}
        USE_TZ = True
        DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
    """))
    return tmp_path


def run_django(harness, db, code):
    """Execute code inside a configured Django process. Raises on failure."""
    env = {**os.environ, "TEST_DB": db, "DJANGO_SETTINGS_MODULE": "settings",
           "PYTHONPATH": str(harness)}
    result = subprocess.run(
        [V2_PYTHON, "-c", "import django; django.setup()\n" + textwrap.dedent(code)],
        cwd=harness, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"django failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


@pytest.fixture
def fresh_db():
    db = "p3_test_fresh"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


@pytest.fixture
def upgraded_db(v1_tag):
    """A real v1 fixture, restored and ready to migrate."""
    db = f"p3_test_up_{v1_tag.replace('.', '_')}"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    subprocess.run(
        ["pg_restore", "-d", db, "--no-owner", "--no-privileges", dump_path(v1_tag)],
        capture_output=True, check=False,
    )
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


def auth_user_fk_count(db):
    return int(psql(db, """
        select count(*) from information_schema.table_constraints tc
        join information_schema.constraint_column_usage ccu
          on tc.constraint_name = ccu.constraint_name
        where tc.constraint_type = 'FOREIGN KEY' and ccu.table_name = 'auth_user'
    """))


# --- fresh install --------------------------------------------------------

def test_fresh_install_creates_the_tables(harness, fresh_db):
    run_django(harness, fresh_db, """
        from django.core.management import call_command
        call_command('migrate', verbosity=0)
    """)
    assert psql(fresh_db, "select count(*) from information_schema.tables "
                          "where table_name='auth_user'") == "1"
    # The point of db_table: no second user table is ever created.
    assert psql(fresh_db, "select count(*) from information_schema.tables "
                          "where table_name='horilla_auth_horillauser'") == "0"


def test_fresh_install_join_tables_use_user_id(harness, fresh_db):
    """Django would derive horillauser_id from the model name; the through
    models force user_id so one schema serves fresh and upgraded databases."""
    run_django(harness, fresh_db, """
        from django.core.management import call_command
        call_command('migrate', verbosity=0)
    """)
    for table in ("auth_user_groups", "auth_user_user_permissions"):
        cols = psql(fresh_db, f"select column_name from information_schema.columns "
                              f"where table_name='{table}'").split()
        assert "user_id" in cols, f"{table} is missing user_id"
        assert "horillauser_id" not in cols


def test_fresh_install_is_functional(harness, fresh_db):
    out = run_django(harness, fresh_db, """
        from django.core.management import call_command
        call_command('migrate', verbosity=0)
        from django.contrib.auth import get_user_model, authenticate
        from django.contrib.auth.models import Group
        U = get_user_model()
        u = U.objects.create_user(username='fresh', password='TestPassw0rd!')
        u.groups.add(Group.objects.create(name='Testers'))
        print(bool(authenticate(username='fresh', password='TestPassw0rd!')))
        print([g.name for g in u.groups.all()])
    """)
    assert "True" in out
    assert "Testers" in out


# --- v1 upgrade -----------------------------------------------------------

def test_upgrade_preserves_every_foreign_key(harness, upgraded_db):
    """F4: users never move, so all ~328 inbound FKs stay valid."""
    before = auth_user_fk_count(upgraded_db)
    assert before > 300

    run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
    """)

    assert auth_user_fk_count(upgraded_db) == before


def test_upgrade_does_not_create_a_second_user_table(harness, upgraded_db):
    run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
    """)
    assert psql(upgraded_db, "select count(*) from information_schema.tables "
                             "where table_name='horilla_auth_horillauser'") == "0"


def test_upgrade_preserves_password_hashes_exactly(harness, upgraded_db):
    """Checked before any login: Django 5.2 raised PBKDF2 iterations from
    600k to 1M and transparently rehashes on successful authentication, so a
    post-login comparison would report a false difference."""
    before = psql(upgraded_db,
                  "select md5(string_agg(password, ',' order by username)) from auth_user")

    run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
    """)

    assert psql(upgraded_db, "select md5(string_agg(password, ',' order by username)) "
                             "from auth_user") == before


def test_upgraded_users_authenticate_with_their_v1_password(harness, upgraded_db):
    out = run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
        from django.contrib.auth import authenticate
        print(bool(authenticate(username='v1user1', password='FixturePassw0rd-1!')))
        print(bool(authenticate(username='v1user1', password='definitely-wrong')))
    """).splitlines()
    assert out[0] == "True", "v1 password stopped working after migration"
    assert out[1] == "False", "a wrong password was accepted"


def test_upgraded_groups_and_permissions_survive(harness, upgraded_db):
    """These read through v1's join tables, whose column is user_id."""
    out = run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
        from django.contrib.auth import get_user_model
        U = get_user_model()
        u = U.objects.get(username='v1user1')
        print([g.name for g in u.groups.all()])
        print(u.user_permissions.count())
        print(U.objects.filter(groups__name='HR Managers').count())
    """).splitlines()
    assert "HR Managers" in out[0]
    assert out[1] == "2"
    assert out[2] == "5"


def test_upgrade_preserves_user_flags(harness, upgraded_db):
    """A migration that silently reactivates a disabled account, or drops a
    superuser bit, is a security problem rather than a data problem."""
    out = run_django(harness, upgraded_db, """
        from django.core.management import call_command
        call_command('migrate', 'horilla_auth', verbosity=0)
        from django.contrib.auth import get_user_model
        U = get_user_model()
        print(U.objects.count())
        print(U.objects.filter(is_superuser=True).count())
        print(U.objects.filter(is_active=False).count())
        print(U.objects.filter(last_login__isnull=True).count())
    """).splitlines()
    assert out == ["10", "1", "1", "5"]
