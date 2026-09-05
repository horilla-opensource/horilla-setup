"""Seed a v1 fixture at customer scale, to measure the migration under load.

Run inside a v1 checkout: `manage.py shell < seed_v1_scale.py`.

Sizes come from the environment so the same script produces a quick smoke
fixture or a full-size one:

    HORILLA_SCALE_EMPLOYEES   default 1000
    HORILLA_SCALE_ATT_DAYS    default 365   (attendance rows = employees * days)
    HORILLA_SCALE_PAYSLIPS    default 12    (per employee)

At the defaults that is ~1k employees, ~365k attendance rows and ~12k
payslips. `HORILLA_SCALE_EMPLOYEES=10000 HORILLA_SCALE_ATT_DAYS=1825` gives
the 10k-employee / 5-year shape the review asked about.

Written with raw SQL and executemany rather than the ORM, unlike
seed_v1_hr_data.py. That is a deliberate departure: v1's model save() methods
fire per-row validation and signal handlers, which makes seeding 365k rows
take hours and measures v1's write path rather than the migration. The
columns written here are the same ones v1's ORM writes -- verified against a
database built by seed_v1_hr_data.py -- so the resulting rows are
schema-identical.

Note on money: v1 stores basic_pay/gross_pay/deduction/net_pay as
`double precision`, not `numeric`. Sums therefore carry float drift
(5041280.999999999 rather than 5041281.00) regardless of what is inserted.
That is v1's schema, not a seeding artefact and not something the migration
introduces -- so assertions compare the before/after sum for exact equality
rather than against a rounded literal.

Generated data only. No production data.
"""

import os
import random
from datetime import date, timedelta

from django.db import connection

EMPLOYEES = int(os.environ.get("HORILLA_SCALE_EMPLOYEES", 1000))
ATT_DAYS = int(os.environ.get("HORILLA_SCALE_ATT_DAYS", 365))
PAYSLIPS_PER_EMP = int(os.environ.get("HORILLA_SCALE_PAYSLIPS", 12))
BATCH = 5000

random.seed(20260905)  # reproducible: the same run twice gives the same rows

cursor = connection.cursor()


def scalar(sql):
    cursor.execute(sql)
    return cursor.fetchone()[0]


def next_id(table):
    return (scalar(f"select coalesce(max(id), 0) from {table}") or 0) + 1


def insert_many(sql, rows):
    """Chunked executemany -- one statement per 5k rows keeps memory flat."""
    for i in range(0, len(rows), BATCH):
        cursor.executemany(sql, rows[i : i + BATCH])


print(f"seeding {EMPLOYEES} employees, {ATT_DAYS} attendance days, "
      f"{PAYSLIPS_PER_EMP} payslips each")

# --- companies and structure ---------------------------------------------
# Two companies, so the migration's company scoping is exercised at scale
# rather than only on the 6-row fixture.
company_base = next_id("base_company")
insert_many(
    "insert into base_company (id, is_active, company, hq, address, country, state, city, zip) "
    "values (%s, true, %s, %s, 'x', 'India', 'Kerala', 'Kochi', '682001')",
    [(company_base + i, f"ScaleCo {i}", i == 0) for i in range(2)],
)
companies = [company_base, company_base + 1]

dept_base = next_id("base_department")
insert_many(
    "insert into base_department (id, is_active, department) values (%s, true, %s)",
    [(dept_base + i, f"Department {i}") for i in range(10)],
)
departments = [dept_base + i for i in range(10)]

pos_base = next_id("base_jobposition")
insert_many(
    "insert into base_jobposition (id, is_active, job_position, department_id_id) "
    "values (%s, true, %s, %s)",
    [(pos_base + i, f"Position {i}", departments[i % len(departments)]) for i in range(20)],
)
positions = [pos_base + i for i in range(20)]

shift_id = next_id("base_employeeshift")
cursor.execute(
    "insert into base_employeeshift (id, is_active, employee_shift, full_time, weekly_full_time) "
    "values (%s, true, 'Scale Shift', '40', '40')",
    [shift_id],
)
worktype_id = next_id("base_worktype")
cursor.execute(
    "insert into base_worktype (id, is_active, work_type) values (%s, true, 'On-site')",
    [worktype_id],
)

# --- users and employees --------------------------------------------------
user_base = next_id("auth_user")
emp_base = next_id("employee_employee")

print("  users...")
insert_many(
    # is_new_employee is NOT NULL with no schema default: v1 adds it via
    # User.add_to_class (base/models.py) rather than a migration, so Django
    # applies the default in Python and a raw INSERT has to supply it.
    "insert into auth_user (id, password, is_superuser, username, first_name, last_name, "
    "email, is_staff, is_active, date_joined, is_new_employee) "
    "values (%s, %s, false, %s, %s, 'Scale', %s, false, true, now(), false)",
    [(user_base + i,
      "pbkdf2_sha256$600000$scalefixture$notarealhashjustpadding0000000000000000000=",
      f"scale{i}", f"Emp{i}", f"scale{i}@example.com")
     for i in range(EMPLOYEES)],
)

print("  employees...")
insert_many(
    "insert into employee_employee (id, employee_first_name, employee_last_name, email, "
    "phone, is_active, employee_user_id_id, is_from_onboarding, is_directly_converted) "
    "values (%s, %s, 'Scale', %s, %s, true, %s, false, false)",
    [(emp_base + i, f"Emp{i}", f"scale{i}@example.com",
      f"9{i:09d}"[:10], user_base + i)
     for i in range(EMPLOYEES)],
)
employees = [emp_base + i for i in range(EMPLOYEES)]

print("  work information...")
info_base = next_id("employee_employeeworkinformation")
insert_many(
    # No is_active on this table -- it does not inherit HorillaModel.
    "insert into employee_employeeworkinformation "
    "(id, employee_id_id, department_id_id, job_position_id_id, company_id_id, "
    " shift_id_id, work_type_id_id, basic_salary, date_joining, experience) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)",
    [(info_base + i, employees[i], departments[i % len(departments)],
      positions[i % len(positions)], companies[i % 2], shift_id, worktype_id,
      30000 + (i % 50) * 1000, date(2023, 1, 1) + timedelta(days=i % 700))
     for i in range(EMPLOYEES)],
)

# --- attendance: the big table -------------------------------------------
# employees * days. At 1000 x 365 that is 365k rows, which is where a
# per-row migration step shows up as minutes rather than seconds.
print(f"  attendance ({EMPLOYEES * ATT_DAYS} rows)...")
att_base = next_id("attendance_attendance")
start = date(2024, 1, 1)
rows = []
next_att = att_base
for day_offset in range(ATT_DAYS):
    day = start + timedelta(days=day_offset)
    for emp in employees:
        rows.append((next_att, day, emp, shift_id, worktype_id))
        next_att += 1
    if len(rows) >= 50000:
        insert_many(
            "insert into attendance_attendance "
            "(id, is_active, attendance_date, employee_id_id, shift_id_id, work_type_id_id, "
            " attendance_clock_in_date, attendance_clock_in, attendance_clock_out_date, "
            " attendance_clock_out, attendance_worked_hour, minimum_hour, attendance_overtime, "
            " attendance_overtime_approve, attendance_validated, approved_overtime_second, "
            " is_validate_request, is_bulk_request, is_validate_request_approved, is_holiday) "
            "values (%s, true, %s, %s, %s, %s, %s, '09:15', %s, '18:30', '09:15', '08:00', "
            " '00:00', false, true, 0, false, false, false, false)",
            [(r[0], r[1], r[2], r[3], r[4], r[1], r[1]) for r in rows],
        )
        rows = []
if rows:
    insert_many(
        "insert into attendance_attendance "
        "(id, is_active, attendance_date, employee_id_id, shift_id_id, work_type_id_id, "
        " attendance_clock_in_date, attendance_clock_in, attendance_clock_out_date, "
        " attendance_clock_out, attendance_worked_hour, minimum_hour, attendance_overtime, "
        " attendance_overtime_approve, attendance_validated, approved_overtime_second, "
        " is_validate_request, is_bulk_request, is_validate_request_approved, is_holiday) "
        "values (%s, true, %s, %s, %s, %s, %s, '09:15', %s, '18:30', '09:15', '08:00', "
        " '00:00', false, true, 0, false, false, false, false)",
        [(r[0], r[1], r[2], r[3], r[4], r[1], r[1]) for r in rows],
    )

# --- payroll: decimals that must survive to the cent ----------------------
print(f"  payslips ({EMPLOYEES * PAYSLIPS_PER_EMP} rows)...")
slip_base = next_id("payroll_payslip")
slips = []
n = slip_base
for month in range(PAYSLIPS_PER_EMP):
    period_start = date(2024, 1, 1) + timedelta(days=30 * month)
    period_end = period_start + timedelta(days=29)
    for i, emp in enumerate(employees):
        gross = 30000 + (i % 50) * 1000 + 0.33
        deduction = round(gross * 0.075, 2)
        slips.append((n, emp, period_start, period_end, gross, deduction,
                      round(gross - deduction, 2), '{"basic": "%.2f"}' % gross))
        n += 1
insert_many(
    "insert into payroll_payslip (id, is_active, employee_id_id, start_date, end_date, "
    " status, basic_pay, gross_pay, deduction, net_pay, pay_head_data, sent_to_employee) "
    "values (%s, true, %s, %s, %s, 'paid', %s, %s, %s, %s, %s::jsonb, false)",
    [(s[0], s[1], s[2], s[3], s[4], s[4], s[5], s[6], s[7]) for s in slips],
)

# Sequences must follow the explicit ids, or the first ORM insert collides.
for table in ("auth_user", "employee_employee", "employee_employeeworkinformation",
              "attendance_attendance", "payroll_payslip", "base_company",
              "base_department", "base_jobposition"):
    cursor.execute(
        "select setval(pg_get_serial_sequence(%s, 'id'), "
        "(select coalesce(max(id), 1) from " + table + "))",
        [table],
    )

print("=== scale fixture seeded ===")
for table in ("base_company", "employee_employee", "employee_employeeworkinformation",
              "attendance_attendance", "payroll_payslip", "auth_user"):
    print(f"{table}={scalar('select count(*) from ' + table)}")
print(f"payslip_net_total={scalar('select sum(net_pay) from payroll_payslip')}")
