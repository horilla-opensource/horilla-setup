"""Seed a v1 fixture database with generated data.

Run via `manage.py shell < seed_v1.py` from inside a v1 checkout (v1 uses
django.contrib.auth.User; v2 uses horilla_auth.HorillaUser, so this cannot be
imported from a v2 process).

Generated data only -- never production data. The shape matters more than the
volume: each row here exists to make a specific migration assertion possible.
"""

from django.contrib.auth.models import Group, Permission, User
from django.utils import timezone

# --- users -------------------------------------------------------------
# Deliberate variety, each covering a case the migration must preserve:
#   user1  superuser + staff        -> privilege flags must survive
#   user2  staff, not superuser     -> partial privilege
#   user10 inactive                 -> must NOT be silently reactivated
#   odd ids have last_login set, even are NULL
#                                   -> nullable datetime handling
# Passwords are per-user so a hash collision cannot mask a copy bug.

groups = {
    "HR Managers": Group.objects.get_or_create(name="HR Managers")[0],
    "Employees": Group.objects.get_or_create(name="Employees")[0],
}

perms = list(Permission.objects.order_by("id")[:6])
groups["HR Managers"].permissions.set(perms[:4])

created = []
for i in range(1, 11):
    user = User.objects.create_user(
        username=f"v1user{i}",
        email=f"v1user{i}@example.com",
        password=f"FixturePassw0rd-{i}!",
        first_name=f"First{i}",
        last_name=f"Last{i}",
    )
    user.is_staff = i <= 2
    user.is_superuser = i == 1
    user.is_active = i != 10
    user.last_login = timezone.now() if i % 2 else None
    user.save()

    user.groups.add(groups["HR Managers"] if i <= 5 else groups["Employees"])
    if i <= 3:
        # Direct user_permissions, distinct from group-granted ones: the
        # migration has to carry both, and conflating them is a real failure.
        user.user_permissions.set(perms[4:6])
    created.append(user)

print(f"users={User.objects.count()} groups={Group.objects.count()}")
print(f"user_group_rows={sum(u.groups.count() for u in created)}")
print(f"user_perm_rows={sum(u.user_permissions.count() for u in created)}")
print(f"sample_hash={User.objects.get(username='v1user1').password[:32]}")
