<div align="center">

# Sovereign Agency OS | Company Engine

**An autonomous, self-governing agency operating system built with Vanilla Python on a headless factory architecture.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Architecture](https://img.shields.io/badge/Architecture-Headless%20Factory-indigo.svg?style=flat-square)](.)
[![Database](https://img.shields.io/badge/Database-Zero--DB%20%7C%20Filesystem%20Native-emerald.svg?style=flat-square)](.)
[![Governance](https://img.shields.io/badge/Governance-L0--L3%20HITL-orange.svg?style=flat-square)](.)
[![Security](https://img.shields.io/badge/Security-Anti--TOCTOU%20%7C%20HMAC-crimson.svg?style=flat-square)](.)
[![License](https://img.shields.io/badge/License-Proprietary-gray.svg?style=flat-square)](.)

<p align="center">
  <a href="#-key-features">Key Features</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-cli-tooling">CLI Tooling</a> •
  <a href="#-governance--security">Governance & Security</a> •
  <a href="#-rest-api">REST API</a>
</p>

---

</div>

## 📖 Overview

**Company Engine** is the orchestration and execution core of **Sovereign Agency OS**. It enables autonomous agents to collaborate across multi-disciplinary business domains without external database bloat or cloud lock-in.

Work in Company Engine executes as **Atomic Deliverable Capsules** — self-contained, auditable units governed by strict filesystem locks, element-level cryptographic gates, and human-in-the-loop (HITL) checkpoints.

```text
[ Incoming Ticket ] 
        │
        ▼
[ Sandbox Trial ] ──(Passed)──► [ Production Spool ] ──► [ Manufacturing Pulse ]
                                                                │
                                                                ▼
[ Published Deliverable ] ◄──(Sign-off)── [ Multi-Gate Review (TOCTOU Sealed) ]
```

---

## ✨ Key Features

- **🛡️ Cryptographic Anti-TOCTOU Protection:** Elements and deliverables are hashed with SHA-256 at human review time. Any unauthorized payload mutation between sign-off and publishing (*Time-of-Check to Time-of-Use*) triggers immediate rejection.
- **⚡ Zero-DB Headless Factory:** State lives natively in structured JSON/JSONL files with strict directory-level locking, supplemented by an ultra-fast, rebuildable SQLite read index (`.read_index.sqlite3`).
- **🎚️ Progressive Autonomy Ladder (L0–L3):**
  - **L0 / L1:** 100% Human approval required before deployment.
  - **L2 / L3:** Autonomous agent sign-off with randomized 2% quality audit sampling.
  - **Fail-Closed Demotion:** Gate rejections instantly demote the domain back to **L0**.
- **🧩 Universal Multi-Gate Governance:** Config-driven sequential gating (`qa_review` → `security_audit` → `domain_lead_approval` → `sovereign_root_approval`) with element-specific hashing.
- **💰 Real-Time Token & Cost Accounting:** Every LLM inference call records prompt/completion tokens and exact USD costs in capsule metadata (`metadata.usage`) and telemetry sidecars.
- **🧠 Cumulative Memory & Rolling Deduplication:** Rejections feed into an append-only negative constraint ledger (`feedback_history.jsonl`). Content is fingerprinted and deduplicated against a rolling domain window.
- **🖥️ Executive Cockpit UI:** A real-time, zero-dependency executive dashboard served as a pure HTML/CSS/Vanilla JS single-page application with keyboard shortcuts (`A` approve, `E` reject) and a `⌘K` command palette.

---

## 🏛️ Architecture

Company Engine adheres to a strict 4-layer separation of concerns:

```text
company-engine/
├── 🧠 engine/            # Immutable Core Engine (stateless, pure business logic)
│   ├── capsule_engine/   # State machine, TOCTOU hashing, 16 constitutional gears
│   ├── orchestration/    # FastAPI server, background pulse, read-index cache, Cockpit UI
│   ├── workers/          # Specialized domain workers (Marketing, QA, Design, Publishing)
│   └── shared_kernel/    # Pydantic schemas, cryptographic contracts, central paths
│
├── 💾 data/              # Stateful Storage (filesystem-native, zero external database)
│   ├── domains/          # 9 business domains with isolated tickets (scratch/ vs approved/)
│   ├── capsules/         # Production capsules (spool/) and trial capsules (sandbox/)
│   ├── assets/           # SHA-256 content-addressed media vault (assets/vault/)
│   ├── governance/       # Element-level approval gates & review queues
│   ├── memory/           # Append-only feedback ledger & rolling deduplication index
│   └── index/            # High-speed SQLite read cache (.read_index.sqlite3)
│
├── ⚙️ config/            # Centralized Configuration Hub
│   ├── company_profile.json  # Brand identity, typography tokens, official color palettes
│   ├── engine.json           # Gate templates, execution timeouts, deduplication rules
│   ├── governance.json       # Domain autonomy ladder definitions (L0–L3)
│   └── marketing_config.json # Channel targets, publishing criteria, domain budgets
│
└── 🚀 ops/               # Operations, Deployment & Tooling
    ├── server_boot.sh    # 1-command server bootstrap and preflight healthcheck (< 2s)
    ├── tools/            # Operator CLI suite (ticket_trial, gears, ticket_mgr, mint)
    ├── deploy/           # Production systemd services, install scripts, preflight probes
    └── bootstrap/        # Containerization specs (compose.yml, up.sh)
```

### Business Domains (9 Verticals)
`analytics` • `client_management` • `design` • `development` • `finance` • `ideation` • `marketing` • `operations` • `people`

---

## 🚀 Quick Start

### Prerequisites
- Python **3.10+** (Python 3.9+ supported)
- Unix-based environment (macOS, Ubuntu, Debian, Rocky Linux)

### 1-Command Boot
Boot the API server, auto-generate `.env` with secure secrets, initialize spool directories, and prime the SQLite index in less than 2 seconds:

```bash
# Start server in background & run healthcheck
bash server_boot.sh

# Check runtime status & PID
bash server_boot.sh --status

# Stop background services
bash server_boot.sh --stop
```

### Access Points
Once booted, the engine is accessible locally:
- **Cockpit Dashboard:** [`http://localhost:8000/`](http://localhost:8000/)
- **Interactive Swagger Docs:** [`http://localhost:8000/docs`](http://localhost:8000/docs)
- **Health Check Probe:** [`http://localhost:8000/api/health`](http://localhost:8000/api/health)

---

## 🛠️ CLI Tooling

Company Engine includes an operator CLI suite located in `ops/tools/`:

### 1. Capsule Sandbox Trials (`ticket_trial.py`)
Run and verify tickets in an isolated offline sandbox without polluting production spools:

```bash
# Run isolated trial for a ticket (zero network side-effects)
python3 ops/tools/ticket_trial.py --ticket <TICKET_ID>

# Run full pipeline trial with auto-approval sign-off
python3 ops/tools/ticket_trial.py --ticket <TICKET_ID> --auto-approve

# List & inspect active sandbox capsules
python3 ops/tools/ticket_trial.py --list
python3 ops/tools/ticket_trial.py --inspect <SANDBOX_CAPSULE_ID>

# Promote a verified sandbox capsule into production spool
python3 ops/tools/ticket_trial.py --promote <SANDBOX_CAPSULE_ID> --by admin
```

### 2. Constitutional Gears Registry (`gears.py`)
Inspect and probe the engine's 16 constitutional worker verbs:

```bash
# List all 16 constitutional gears and active endpoints
python3 ops/tools/gears.py list

# Probe response latency of an individual gear
python3 ops/tools/gears.py probe text:review
python3 ops/tools/gears.py probe social:publish

# Probe all 16 gears simultaneously
python3 ops/tools/gears.py probe-all
```

### 3. Ticket Lifecycle Management (`ticket_mgr.py`)
Manage ticket stages between draft sandbox and production queues:

```bash
# List tickets by stage
python3 -m ops.tools.ticket_mgr list --stage scratch
python3 -m ops.tools.ticket_mgr list --stage approved

# Promote a draft ticket to the manufacturing spool
python3 -m ops.tools.ticket_mgr promote <TICKET_ID> --sector marketing
```

### 4. Scoped Capability Minting (`mint_capability.py`)
Issue stateless, HMAC-signed capability tokens with fine-grained scoping:

```bash
# Mint a human reviewer key scoped to marketing sign-offs (valid 7 days)
python3 ops/tools/mint_capability.py mint --subject abdallah \
    --capabilities capsule:approve:marketing,gate:approve:marketing \
    --ttl-days 7

# Inspect and verify a capability token offline
python3 ops/tools/mint_capability.py inspect <TOKEN>
```

---

## 🔒 Governance & Security

### Multi-Gate Approval Pipeline
Capsules progress through dynamic, multi-stage approval plans declared in `config/engine.json`:

| Gate Type | Responsibility | Target Bound |
| :--- | :--- | :--- |
| `qa_review` | Automated structural, copy, and brand compliance checks | `deliverables/**/*` |
| `security_audit` | Content safety, secrets detection, and license audit | Entire capsule payload |
| `domain_lead_approval` | Domain-specific human sign-off (scoped authority) | Element deliverables |
| `sovereign_root_approval` | Final executive authorization (optional / high-risk tasks) | Root manifest |

### Security Foundations
- **Default Deny:** Every API route requires verified capability tokens. Legacy reads enforce the engine-wide `factory:read`; `/v1` reads are **sector-scoped** — `domain:read:<sector>` (or `domain:read:*` / root for every sector) — so a mobile caller sees only the domains it is granted, and an out-of-scope object answers `404`, never `403`. Mutations require resource-scoped permissions (`capsule:approve:<sector>`).
- **Offline Signature Verification:** All capability tokens are validated via HMAC-SHA256 statelessly — eliminating central database auth bottlenecks.
- **Fail-Closed Design:** Any hash mismatch, unexpected schema variation, or expired token halts execution immediately and surfaces a human review signal.

---

## 📡 REST API & Telemetry

Core HTTP endpoints exposed by the orchestration server:

| Method | Endpoint | Capability Required | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/health` | *Public* | Engine and worker runtime health check |
| `GET` | `/api/tasks/in-flight` | `factory:read` | Real-time queue telemetry, active gear, tokens & spend |
| `GET` | `/api/radar/tasks` | `factory:read` | High-level executive radar across all 9 domains |
| `GET` | `/api/capsules/{id}/gates` | `factory:read` | Full gate plan, checklist status, and audit evidence |
| `POST` | `/api/capsules/{id}/gates/{gate_id}/decision` | `capsule:approve` | Approve, reject, or skip a specific gate |
| `GET` | `/api/capsules/{id}/trace` | `factory:read` | Hash-chained execution trace and event log |
| `GET` | `/api/capsules/{id}/preview` | `factory:read` | Production delivery preview of deliverables |
| `GET` | `/api/memory/feedback` | `factory:read` | Cumulative rejection history and negative constraints |

---

## 📄 License

Proprietary & Confidential. Copyright © Sovereign Agency OS. All rights reserved.
