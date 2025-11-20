# 🛠️ Horilla Setup CLI (`horillasetup`)

The **Horilla Setup CLI** is a lightweight, cross-platform command-line tool designed to streamline the **initialization**, **migration**, **upgrade**, and **dependency management** processes across the **Horilla ecosystem** — including **HRMS v1**, **HRMS v2**, and the newly released **Horilla CRM**.

It automates repetitive setup tasks like environment preparation, Git cloning, dependency installation, and migration handling — ensuring a smooth, consistent workflow for developers and deployment teams.

---

## 🚀 Key Features

✓ **Quick project setup** for HRMS (v1 & v2) and CRM
✓ **Version-aware migrations** including HRMS v1 → v2 upgrade
✓ **Automated dependency installation** from `requirements.txt`
✓ **Seamless project upgrades** via Git pull
✓ **Cross-platform support** (Windows, Linux, macOS)
✓ **Single command workflow** for setup, migration, and updates

---

## ⚙️ Installation

### 📦 Global Installation (Recommended)

```bash
pip install horillasetup
```

### 🧩 Local Development Installation

If you’re improving or modifying the tool:

```bash
git clone https://github.com/horilla-opensource/setup.git
cd horilla-ctl
pip install -e .
```

> `-e` installs the package in *editable mode*, so changes take effect instantly.

---

## 🧭 Usage Guide

Show all available commands:

```bash
horillasetup --help
```

---

# 🏗️ 1. Build a New Horilla Project

### HRMS v1

```bash
horillasetup build hrms-v1
```

### HRMS v2

```bash
horillasetup build hrms-v2
```

### CRM (Newly Released 🚀)

```bash
horillasetup build crm
```

**The build command will:**

* Clone the correct Horilla repo (branch-specific)
* Copy project files into the working directory
* Install Python dependencies
* Provide environment setup instructions

---

# 🧱 2. Run Migrations

### HRMS v1

```bash
horillasetup migrate hrms-v1
```

### HRMS v2

```bash
horillasetup migrate hrms-v2
```

### CRM

```bash
horillasetup migrate crm
```

**Migration steps include:**

* Running `makemigrations`
* Applying migrations
* Collecting static files

---

# 🔄 3. Upgrade an Existing Project

Pull latest code updates from Git:

### HRMS v1

```bash
horillasetup upgrade hrms-v1
```

### HRMS v2

```bash
horillasetup upgrade hrms-v2
```

### CRM

```bash
horillasetup upgrade crm
```

---

# 🔁 4. HRMS v1 → HRMS v2 Database Upgrade

To migrate an existing v1 database into v2:

```bash
horillasetup migrate hrms-v2 --existing
```

This performs:

* Safe clearing of old migration entries
* Faked compatibility migrations
* Full v2 migration + static collection

---

# 📦 5. Install Dependencies Only

```bash
horillasetup install-deps
```

Installs all packages from `requirements.txt`.

---

## 💡 Example Setup Workflow

```bash
# Build a fresh CRM project
horillasetup build crm

# Run CRM migrations
horillasetup migrate crm

# Upgrade project later
horillasetup upgrade crm
```

---

## 🛣️ Future Roadmap

* 🔌 Plugin-based scaffolding for new Horilla modules
* 🔍 Automated version & dependency conflict detection
* 📦 Project template generator
* 🧰 Extended DevOps tools integration