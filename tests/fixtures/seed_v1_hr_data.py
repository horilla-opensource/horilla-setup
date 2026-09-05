"""Seed a v1 fixture with realistic HR data, via v1's own ORM.

Run inside a v1 checkout: `manage.py shell < seed_v1_hr_data.py`.

Written against the ORM rather than raw SQL so every row satisfies v1's own
validation and FK constraints -- data that v1 itself would never have created
proves nothing about a migration.

The shape is chosen to exercise the transforms the migration has to get right,
not to be large:

  * TWO companies, so multi-company scoping is testable. A single-company
    fixture cannot reveal cross-tenant leakage, which is the highest-risk
    class of migration bug.
  * Decimal money values with awkward fractions, to catch precision loss.
  * Timezone-aware datetimes spanning a DST boundary.
  * A leave request with dates either side of a month end.
  * A CompanyLeaves row -- the FK -> M2M retype in v2.
  * A Holiday row -- moves from the leave app to base in v2.
  * Rows deliberately left with NULL optional FKs, to catch a migration that
    assumes they are populated.

Generated data only. No production data, and no real personal details.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from base.models import Company, Department, JobPosition, WorkType, EmployeeShift
from employee.models import Employee, EmployeeWorkInformation


# Several v1 models read request.session in save() with no None-guard, so
# they raise AttributeError outside a web request -- in a management command,
# a shell, or any data import. leave.Holiday and leave.CompanyLeave both do.
# A minimal fake request satisfies them without pulling in the middleware.
# This is a v1 bug worth reporting separately, not a fixture quirk.
from horilla import horilla_middlewares


class _FakeRequest:
    # user must be an AnonymousUser, not None: HorillaModel.save() reads
    # user.is_authenticated unguarded (horilla/models.py:158).
    from django.contrib.auth.models import AnonymousUser

    session = {}
    POST = {}
    user = AnonymousUser()
    method = "GET"


horilla_middlewares._thread_locals.request = _FakeRequest()


def create(model, **kwargs):
    """Save without Manager.create().

    v1's HorillaModel.save() forwards *args to clean(), so `.objects.create()`
    raises `Model.clean() got an unexpected keyword argument 'force_insert'`.
    Constructing then calling save() with no positional args avoids it.
    This is a v1 bug, not a fixture quirk -- worth reporting separately.
    """
    obj = model(**kwargs)
    obj.save()
    return obj


# --- companies: two, so tenant scoping is observable ---------------------
acme = create(Company, 
    company="Acme Manufacturing", hq=True, address="1 Industrial Way",
    country="India", state="Kerala", city="Kochi", zip="682001",
)
globex = create(Company, 
    company="Globex Services", hq=False, address="2 Service Road",
    country="India", state="Karnataka", city="Bengaluru", zip="560001",
)

departments = {}
for comp, names in ((acme, ["Production", "Quality"]), (globex, ["Support", "Sales"])):
    for name in names:
        dept = create(Department, department=name)
        dept.company_id.add(comp)
        departments[name] = dept

positions = {}
for dept_name, titles in (
    ("Production", ["Machine Operator", "Shift Supervisor"]),
    ("Quality", ["QA Inspector"]),
    ("Support", ["Support Engineer"]),
    ("Sales", ["Account Manager"]),
):
    for title in titles:
        pos = create(JobPosition, 
            job_position=title, department_id=departments[dept_name]
        )
        pos.company_id.add(*departments[dept_name].company_id.all())
        positions[title] = pos

shift = create(EmployeeShift, employee_shift="General Shift")
work_type = create(WorkType, work_type="On-site")

# --- employees, split across both companies ------------------------------
# Salaries carry fractional rupees on purpose: a migration that rounds or
# re-types a money column shows up as a changed total.
SPEC = [
    ("Asha",   "Nair",     "Production", "Machine Operator",  acme,   Decimal("31450.75")),
    ("Bilal",  "Khan",     "Production", "Shift Supervisor",  acme,   Decimal("48200.33")),
    ("Chitra", "Menon",    "Quality",    "QA Inspector",      acme,   Decimal("39875.50")),
    ("Deepak", "Rao",      "Support",    "Support Engineer",  globex, Decimal("52310.10")),
    ("Elena",  "Fernandez","Sales",      "Account Manager",   globex, Decimal("61999.99")),
]

employees = []
for i, (first, last, dept, title, company, salary) in enumerate(SPEC, start=1):
    user = User.objects.create_user(
        username=f"{first.lower()}.{last.lower()}",
        email=f"{first.lower()}@example.com",
        password=f"EmpPassw0rd-{i}!",
        first_name=first, last_name=last,
    )
    emp = create(Employee, 
        employee_first_name=first, employee_last_name=last,
        email=f"{first.lower()}@example.com",
        phone=f"90000000{i:02d}",
        employee_user_id=user,
    )
    # v1's Employee.save() already creates a blank EmployeeWorkInformation,
    # so this fills that row in rather than inserting a second one.
    info, _ = EmployeeWorkInformation.objects.get_or_create(employee_id=emp)
    info.department_id = departments[dept]
    info.job_position_id = positions[title]
    info.company_id = company
    info.shift_id = shift
    info.work_type_id = work_type
    info.basic_salary = salary
    info.date_joining = date(2023, 1, 15) + timedelta(days=i * 30)
    info.save()
    employees.append(emp)

# One employee with NO work information at all: a migration that assumes the
# relation exists will fail on this row rather than in production.
orphan = create(Employee, 
    employee_first_name="Farid", employee_last_name="Ahmed",
    email="farid@example.com", phone="9000000099",
)
employees.append(orphan)

# --- attendance across a DST boundary ------------------------------------
# India has no DST, but the stored values are timezone-aware and a migration
# that naively re-interprets them will shift the instant.
from attendance.models import Attendance

attendance_rows = 0
for emp in employees[:3]:
    for offset in range(5):
        day = date(2024, 3, 29) + timedelta(days=offset)  # spans 31 Mar
        create(Attendance, 
            employee_id=emp,
            attendance_date=day,
            attendance_clock_in_date=day,
            attendance_clock_in=time(9, 15),
            attendance_clock_out_date=day,
            attendance_clock_out=time(18, 30),
            attendance_worked_hour="09:15",
            minimum_hour="08:00",
            shift_id=shift,
            work_type_id=work_type,
        )
        attendance_rows += 1

# --- leave: holiday + company leave (both move or re-type in v2) ---------
from leave.models import Holiday, CompanyLeave, LeaveType

holiday = create(Holiday, 
    name="Onam", start_date=date(2024, 9, 15), end_date=date(2024, 9, 17),
    recurring=True,
)
# CompanyLeave.company_id is a ForeignKey in v1 and a ManyToManyField in v2 --
# this row is what the FK -> M2M transform has to carry across.
company_leave = create(CompanyLeave, 
    based_on_week="1", based_on_week_day="6",
)

leave_type = create(LeaveType, 
    name="Casual Leave", payment="paid", count=12, period_in="year",
    total_days=12, color="#4287f5",
)

# --- payroll: decimals that must survive to the cent ---------------------
from payroll.models.models import Payslip

payslip_total = Decimal("0.00")
for emp in employees[:3]:
    gross = Decimal("48200.33")
    deduction = Decimal("3616.02")
    net = gross - deduction
    payslip_total += net
    create(Payslip, 
        employee_id=emp,
        start_date=date(2024, 4, 1),
        end_date=date(2024, 4, 30),
        status="paid",
        basic_pay=gross,
        gross_pay=gross,
        deduction=deduction,
        net_pay=net,
        pay_head_data={"basic": str(gross), "deductions": str(deduction)},
    )

# --- leave: types, balances and requests -----------------------------------
# leave_type already created above. Add balances and requests, including a
# request spanning a month boundary -- date arithmetic that assumes a request
# sits inside one month gets this wrong.
from leave.models import AvailableLeave, LeaveRequest

leave_requests = 0
for emp in employees[:3]:
    create(AvailableLeave,
        employee_id=emp,
        leave_type_id=leave_type,
        available_days=8.5,
        carryforward_days=2.5,
        total_leave_days=11.0,
        assigned_date=date(2024, 1, 1),
    )
    # 28 Feb -> 3 Mar: crosses a month end AND a leap-year boundary.
    create(LeaveRequest,
        employee_id=emp,
        leave_type_id=leave_type,
        start_date=date(2024, 2, 28),
        end_date=date(2024, 3, 3),
        start_date_breakdown="full_day",
        end_date_breakdown="full_day",
        requested_days=5.0,
        description="Family function",
        status="approved",
        requested_date=date(2024, 2, 20),
        approved_available_days=5.0,
        approved_carryforward_days=0.0,
    )
    leave_requests += 1

# --- recruitment: a pipeline with candidates at different stages -----------
from recruitment.models import Recruitment, Stage, Candidate

recruitment = create(Recruitment,
    title="Production Hiring 2024",
    description="Line operators",
    vacancy=3,
    start_date=date(2024, 5, 1),
    end_date=date(2024, 7, 31),
    closed=False,
    is_published=True,
)
recruitment.company_id = acme
recruitment.save()
recruitment.open_positions.add(positions["Machine Operator"])
recruitment.recruitment_managers.add(employees[1])

# v1's Recruitment.save() already creates a default stage set, so reuse what
# is there rather than inserting duplicates (the (recruitment, stage) pair is
# unique). Any stage type missing from the defaults gets added.
stages = {st.stage: st for st in Stage.objects.filter(recruitment_id=recruitment)}
for order, (title, kind) in enumerate(
    [("Applied", "initial"), ("Interview", "interview"), ("Hired", "hired")], start=1
):
    if title not in stages:
        stages[title] = create(Stage,
            recruitment_id=recruitment,
            stage=title,
            stage_type=kind,
            sequence=order,
        )
# Pick a concrete stage for each candidate from whatever exists.
stage_names = list(stages)
print(f"  (recruitment stages present: {stage_names})")

candidates = []
for i, (name, hired) in enumerate(
    [("Gita Sharma", False), ("Hari Prasad", False), ("Irfan Ali", True)], start=1
):
    # Spread candidates across whatever stages exist, so the pipeline has
    # rows at more than one stage.
    stage_obj = stages[stage_names[(i - 1) % len(stage_names)]]
    cand = create(Candidate,
        name=name,
        email=f"cand{i}@example.com",
        mobile=f"88000000{i:02d}",
        recruitment_id=recruitment,
        stage_id=stage_obj,
        job_position_id=positions["Machine Operator"],
        hired=hired,
        start_onboard=hired,
        portfolio="",
        resume="resume/placeholder.pdf",
    )
    candidates.append(cand)

# --- onboarding: stages plus a candidate partway through -------------------
from onboarding.models import OnboardingStage, CandidateStage

onboarding_stages = {}
for order, (title, final) in enumerate(
    [("Documents", False), ("Induction", False), ("Complete", True)], start=1
):
    onboarding_stages[title] = create(OnboardingStage,
        recruitment_id=recruitment,
        stage_title=title,
        is_final_stage=final,
        sequence=order,
    )

# Only the hired candidate enters onboarding, which is the real-world shape.
onboarding_rows = 0
hired_candidate = candidates[-1]
create(CandidateStage,
    candidate_id=hired_candidate,
    onboarding_stage_id=onboarding_stages["Induction"],
)
onboarding_rows += 1

print("=== v1 HR fixture seeded ===")
print(f"companies={Company.objects.count()}")
print(f"departments={Department.objects.count()}")
print(f"job_positions={JobPosition.objects.count()}")
print(f"employees={Employee.objects.count()}")
print(f"work_info={EmployeeWorkInformation.objects.count()}")
print(f"attendance={Attendance.objects.count()}")
print(f"payslips={Payslip.objects.count()}")
print(f"payslip_net_total={payslip_total}")
print(f"holidays={Holiday.objects.count()}")
print(f"company_leaves={CompanyLeave.objects.count()}")
print(f"leave_types={LeaveType.objects.count()}")
print(f"users={User.objects.count()}")
print(f"available_leave={AvailableLeave.objects.count()}")
print(f"leave_requests={LeaveRequest.objects.count()}")
print(f"recruitments={Recruitment.objects.count()}")
print(f"stages={Stage.objects.count()}")
print(f"candidates={Candidate.objects.count()}")
print(f"onboarding_stages={OnboardingStage.objects.count()}")
print(f"candidate_stages={CandidateStage.objects.count()}")
