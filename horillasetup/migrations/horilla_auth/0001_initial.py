"""horilla_auth.0001_initial, served from the setup tool rather than the app.

Reached via MIGRATION_MODULES in horillasetup/migration_settings.py. Django
keys migrations by (app_label, name), so to the rest of the project this IS
horilla_auth's initial migration -- it just lives somewhere the HR codebase
does not have to carry.

Two starting points, one migration:

  fresh install   auth_user does not exist -> create the tables normally
  v1 upgrade      auth_user exists with rows and ~328 inbound foreign keys
                  -> rename it, so nothing moves and no key is rewritten

SeparateDatabaseAndState is what makes that possible: Django's migration state
advances identically either way, while the physical work is chosen at runtime
by looking at the database.

WHY RENAME RATHER THAN COPY. v1's auth_user has ~328 inbound foreign keys.
Copying users to a new table means rewriting every one of them; a rename is a
catalogue update that Postgres propagates to all 328 automatically, in constant
time, with no window where a key points at the wrong row.

WHY THREE RENAMES. Django derives M2M join-table names AND their join column
names from the MODEL name, not from db_table. So renaming only auth_user gets
you `relation "horilla_auth_horillauser_groups" does not exist`, and fixing
that alone then gets you a missing horillauser_id column. Found by running it,
in that order.
"""

import django.contrib.auth.models
import django.contrib.auth.validators
import django.utils.timezone
from django.db import migrations, models

# v1's names -> what Django expects for a HorillaUser model in app horilla_auth.
TABLE_RENAMES = [
    ("auth_user", "horilla_auth_horillauser"),
    ("auth_user_groups", "horilla_auth_horillauser_groups"),
    ("auth_user_user_permissions", "horilla_auth_horillauser_user_permissions"),
]

# Django names a join FK after the model, not the table it points at.
COLUMN_RENAMES = [
    ("horilla_auth_horillauser_groups", "user_id", "horillauser_id"),
    ("horilla_auth_horillauser_user_permissions", "user_id", "horillauser_id"),
]


def _table_exists(cursor, name):
    cursor.execute(
        "select 1 from information_schema.tables "
        "where table_schema = current_schema() and table_name = %s",
        [name],
    )
    return cursor.fetchone() is not None


def _column_exists(cursor, table, column):
    cursor.execute(
        "select 1 from information_schema.columns "
        "where table_schema = current_schema() "
        "and table_name = %s and column_name = %s",
        [table, column],
    )
    return cursor.fetchone() is not None


def adopt_v1_tables(apps, schema_editor):
    """Rename v1's auth tables into the names HorillaUser expects.

    A no-op on a fresh install, where auth_user was never created. Each step is
    guarded independently rather than assuming all three succeed or fail
    together: a previous run that died midway must be resumable.
    """
    with schema_editor.connection.cursor() as cursor:
        for old, new in TABLE_RENAMES:
            if _table_exists(cursor, old) and not _table_exists(cursor, new):
                cursor.execute(f'alter table "{old}" rename to "{new}"')

        for table, old_col, new_col in COLUMN_RENAMES:
            if _table_exists(cursor, table) and _column_exists(cursor, table, old_col):
                cursor.execute(
                    f'alter table "{table}" rename column "{old_col}" to "{new_col}"'
                )


def create_if_fresh(apps, schema_editor):
    """Create the tables normally when there was no v1 database to adopt.

    Without this a fresh install ends up with NO user table at all: the state
    operations below advance Django's model state regardless, so the physical
    CreateModel that would normally accompany them never happens. Found by
    running the fresh-install path rather than assuming it mirrored the
    upgrade path.

    Uses the live app registry, not `apps`: the historical registry cannot
    resolve a model that this same migration is introducing.
    """
    from django.apps import apps as global_apps

    model = global_apps.get_model("horilla_auth", "HorillaUser")
    with schema_editor.connection.cursor() as cursor:
        if _table_exists(cursor, model._meta.db_table):
            return

    schema_editor.create_model(model)
    for field in model._meta.local_many_to_many:
        if field.remote_field.through._meta.auto_created:
            schema_editor.create_model(field.remote_field.through)


def unadopt(apps, schema_editor):
    """Reverse the rename, so `migrate horilla_auth zero` restores v1's names.

    Only meaningful for a database that was adopted; on a fresh install the
    tables carry the v2 names legitimately and reversing would invent an
    auth_user that never existed. Distinguished by the join column: v1 named it
    user_id, Django names it horillauser_id.
    """
    with schema_editor.connection.cursor() as cursor:
        for table, old_col, new_col in COLUMN_RENAMES:
            if _table_exists(cursor, table) and _column_exists(cursor, table, new_col):
                cursor.execute(
                    f'alter table "{table}" rename column "{new_col}" to "{old_col}"'
                )

        for old, new in TABLE_RENAMES:
            if _table_exists(cursor, new) and not _table_exists(cursor, old):
                cursor.execute(f'alter table "{new}" rename to "{old}"')


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Physical: adopt v1's tables, or create fresh ones. Order matters
            # -- create_if_fresh must see the result of the rename, so that it
            # correctly does nothing on an upgrade.
            database_operations=[
                migrations.RunPython(adopt_v1_tables, unadopt),
                migrations.RunPython(create_if_fresh, migrations.RunPython.noop),
            ],
            # State: byte-for-byte upstream's own operations, copied at build
            # time rather than retyped, so this cannot drift from the app's
            # models.py. The three managed=False models emit no DDL and so
            # need no physical counterpart above.
            state_operations=[

            migrations.CreateModel(
                name="AuthUserGroups",
                fields=[
                    (
                        "id",
                        models.BigAutoField(
                            auto_created=True,
                            primary_key=True,
                            serialize=False,
                            verbose_name="ID",
                        ),
                    ),
                ],
                options={
                    "db_table": "auth_user_groups",
                    "managed": False,
                },
            ),
            migrations.CreateModel(
                name="AuthUserUserPermissions",
                fields=[
                    (
                        "id",
                        models.BigAutoField(
                            auto_created=True,
                            primary_key=True,
                            serialize=False,
                            verbose_name="ID",
                        ),
                    ),
                ],
                options={
                    "db_table": "auth_user_user_permissions",
                    "managed": False,
                },
            ),
            migrations.CreateModel(
                name="LegacyUser",
                fields=[
                    ("id", models.BigAutoField(primary_key=True, serialize=False)),
                    ("username", models.CharField(max_length=150)),
                    ("password", models.CharField(max_length=128)),
                    ("first_name", models.CharField(blank=True, max_length=150)),
                    ("last_name", models.CharField(blank=True, max_length=150)),
                    ("email", models.EmailField(blank=True, max_length=254)),
                    ("is_staff", models.BooleanField(default=False)),
                    ("is_active", models.BooleanField(default=True)),
                    ("is_superuser", models.BooleanField(default=False)),
                    ("last_login", models.DateTimeField(blank=True, null=True)),
                    ("date_joined", models.DateTimeField(blank=True, null=True)),
                ],
                options={
                    "db_table": "auth_user",
                    "managed": False,
                },
            ),
            migrations.CreateModel(
                name="HorillaUser",
                fields=[
                    (
                        "id",
                        models.BigAutoField(
                            auto_created=True,
                            primary_key=True,
                            serialize=False,
                            verbose_name="ID",
                        ),
                    ),
                    ("password", models.CharField(max_length=128, verbose_name="password")),
                    (
                        "last_login",
                        models.DateTimeField(
                            blank=True, null=True, verbose_name="last login"
                        ),
                    ),
                    (
                        "is_superuser",
                        models.BooleanField(
                            default=False,
                            help_text="Designates that this user has all permissions without explicitly assigning them.",
                            verbose_name="superuser status",
                        ),
                    ),
                    (
                        "username",
                        models.CharField(
                            error_messages={
                                "unique": "A user with that username already exists."
                            },
                            help_text="Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.",
                            max_length=150,
                            unique=True,
                            validators=[
                                django.contrib.auth.validators.UnicodeUsernameValidator()
                            ],
                            verbose_name="username",
                        ),
                    ),
                    (
                        "first_name",
                        models.CharField(
                            blank=True, max_length=150, verbose_name="first name"
                        ),
                    ),
                    (
                        "last_name",
                        models.CharField(
                            blank=True, max_length=150, verbose_name="last name"
                        ),
                    ),
                    (
                        "email",
                        models.EmailField(
                            blank=True, max_length=254, verbose_name="email address"
                        ),
                    ),
                    (
                        "is_staff",
                        models.BooleanField(
                            default=False,
                            help_text="Designates whether the user can log into this admin site.",
                            verbose_name="staff status",
                        ),
                    ),
                    (
                        "is_active",
                        models.BooleanField(
                            default=True,
                            help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.",
                            verbose_name="active",
                        ),
                    ),
                    (
                        "date_joined",
                        models.DateTimeField(
                            default=django.utils.timezone.now, verbose_name="date joined"
                        ),
                    ),
                    ("is_new_employee", models.BooleanField(default=False)),
                    (
                        "groups",
                        models.ManyToManyField(
                            blank=True,
                            help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                            related_name="user_set",
                            related_query_name="user",
                            to="auth.group",
                            verbose_name="groups",
                        ),
                    ),
                    (
                        "user_permissions",
                        models.ManyToManyField(
                            blank=True,
                            help_text="Specific permissions for this user.",
                            related_name="user_set",
                            related_query_name="user",
                            to="auth.permission",
                            verbose_name="user permissions",
                        ),
                    ),
                ],
                options={
                    "verbose_name": "User",
                    "verbose_name_plural": "Users",
                    "swappable": "AUTH_USER_MODEL",
                },
                managers=[
                    ("objects", django.contrib.auth.models.UserManager()),
                ],
            ),
            ],
        ),
    ]
