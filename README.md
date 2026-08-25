# LocalApply

A local-first autonomous job-application workstation. Perception, reasoning, execution, and
user control are four separate layers, not one model with a browser attached.

```
    OBSERVE  ──►  REASON  ──►  POLICY  ──►  EXECUTE  ──►  OBSERVE ──┐
   (Playwright)    (LLM)    (plain code)  (Playwright)              │
        ▲                                                          │
        └──────────────────────────────────────────────────────────┘
```

The reasoner proposes; it holds no browser handle and cannot act. The policy engine — plain
Python, no model — decides. The executor performs mechanical Playwright operations and
interprets nothing. Every step is appended to a log that drives both the live dashboard and
offline replay.

**Status: the loop, your CV, and your documents.** The whole agent loop runs end to end
against a local HTML fixture, driven either by a deterministic scripted reasoner or by a
local model through Ollama. Your CV is parsed into individually-approved facts you can
correct by hand, and tailored CVs and cover letters are generated from them — grounded, so
every line traces to a fact you accepted. Real job sites are still out of scope on purpose;
see [Not built yet](#not-built-yet).

- [Architecture](docs/architecture.md)
- [Agent protocol](docs/agent-protocol.md)
- [ADR 0001 — why the loop is split four ways](docs/adr/0001-observe-reason-policy-execute.md)
- [ADR 0002 — why the model never sees a selector](docs/adr/0002-opaque-element-refs.md)
- [ADR 0003 — a model may rephrase a fact, never author a claim](docs/adr/0003-model-is-a-rewriter.md)

---

## Safety properties

These are enforced structurally and covered by tests, not asserted in a prompt:

- **No application is ever submitted without a human approving that exact action.**
  `SUBMIT` is its own action type, gated by rule `R010`, and `SUBMITTING` is reachable from
  exactly one state (`REVIEW_REQUIRED`). Approving one action does not authorise a different
  value or a different element.
- **The model cannot address anything the observer did not enumerate.** It emits opaque refs
  (`e17`), never selectors or coordinates. Refs are rebuilt every observation, so stale ones
  fail closed. See ADR 0002.
- **Signatures, government IDs, demographic questions, and credentials are never filled** —
  not at any confidence, and not unlockable by approval. They are left for you, and the run
  tells you which ones before it submits.
- **Page content cannot weaken policy.** The policy engine contains no model, so a prompt
  injection can at most produce a bad proposal, which policy then rejects.
- **`DRY_RUN=true` by default.** Submits are logged and simulated but never actually clicked.
- **The kill switch is checked before every action**, by both the run loop and the executor.

## Requirements

Nothing on this list is installed by default on Windows. Steps 4–5 need a reboot.

1. **Disable the Windows Store Python aliases** — Settings → Apps → Advanced app settings →
   App execution aliases → turn off `python.exe` and `python3.exe`. The default `python` on
   PATH is a Store stub, not an interpreter.
2. **Python 3.12 or newer** from python.org, with "Add to PATH" ticked.
   Verified on 3.14.7 — every dependency, including `asyncpg` and `greenlet`, has cp314
   wheels.
3. **Node 20 LTS**, then `corepack enable`. Needed *only* for the dashboard in `apps/web`;
   the API, the agent, and the whole test suite run without it.
4. `wsl --install` from an admin PowerShell, then **reboot**.
5. **Docker Desktop** with the WSL2 backend.

Verify each on its own line (PowerShell has no `&&`):

```powershell
python --version
node -v
pnpm -v
docker compose version
```

## Running it

Once set up, starting the app is **one double-click**: `LocalApply.exe` at the repo root.

It checks Docker, brings up Postgres and Redis, applies any pending migration, seeds the
profile if needed, starts the API, and opens the dashboard — reporting each step, and saying
exactly what to do if one fails. Ctrl+C stops the API and engages the kill switch on the way
out, so no run is left half-way through a form. The containers stay up so the next start is
quick.

```
[1/6] Docker
      OK  Docker engine 29.7.2
[2/6] Postgres + Redis
      OK  Postgres and Redis healthy
...
[6/6] API + dashboard
      OK  API listening on port 8000  [DRY RUN]

  Ready  http://127.0.0.1:8000/
```

Flags: `--port <n>`, `--no-browser`, `--no-seed`. Rebuild it after changing the launcher:

```powershell
.\services\api\.venv\Scripts\python.exe launcher\build.py
```

**The dashboard needs no Node.** The API serves a zero-build Mission Control at `/` — the
same status strip, live event stream, screenshot view, and approval cards as the React app,
in one dependency-free HTML file. If you later run `pnpm build` in `apps/web`, the built
React app takes over at `/` automatically.

## Setup

Only needed once, and `.\dev.ps1 setup` does all of it in one command.


> **Call every tool as `.venv\Scripts\python.exe -m <tool>`.**
> Not `activate` then `alembic`. Activation is easy to skip silently — and if you do, `pip
> install` lands in your *global* Python and the `alembic` / `uvicorn` / `playwright`
> commands resolve to nothing, because their `.exe` shims go to a Scripts directory that is
> not on PATH. Invoking through the venv's own interpreter needs no activation, no PATH
> edit, and is unaffected by PowerShell's execution policy.

### PowerShell

Windows PowerShell 5.1 has **no `&&` operator** — a line containing it fails to parse and
*nothing on that line runs*. Use one command per line.

```powershell
# from the repo root

# 1. Postgres + Redis (the app itself runs natively; see docs/architecture.md)
docker compose -f infrastructure/docker/docker-compose.yml up -d

# 2. Python environment
cd services\api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium

# 3. Config, then schema. The first migration is autogenerated against the live database.
Copy-Item ..\..\.env.example ..\..\.env
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "initial schema"
.\.venv\Scripts\python.exe -m alembic upgrade head

# 4. Seed a profile
.\.venv\Scripts\python.exe scripts\dev_bootstrap.py

# 5. API — leave this running. Note: no --reload, see below.
.\.venv\Scripts\python.exe -m uvicorn localapply.main:app --port 8000
```

> **Never start the API with `--reload` on Windows.** Reload mode runs uvicorn on a
> `SelectorEventLoop`, and `asyncio.create_subprocess_exec` is not implemented there — so
> Playwright cannot spawn its driver and every run dies with a bare `NotImplementedError`.
> The API itself looks perfectly healthy. `dev.ps1` and the launcher both refuse to pass the
> flag, and `/health` reports the problem if you somehow end up on the wrong loop.

Dashboard, in a second terminal **from the repo root** (needs Node):

```powershell
cd apps\web
pnpm install
pnpm dev
```

Tests, from the repo root:

```powershell
.\services\api\.venv\Scripts\python.exe -m pytest
```

### Git Bash / WSL

```bash
docker compose -f infrastructure/docker/docker-compose.yml up -d

cd services/api
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
./.venv/Scripts/python.exe -m playwright install chromium

cp ../../.env.example ../../.env
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "initial schema"
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe scripts/dev_bootstrap.py
./.venv/Scripts/python.exe -m uvicorn localapply.main:app --port 8000   # no --reload, see above
```

Open <http://localhost:5173>.

### If you already installed into your global Python

Harmless, but it shadows nothing useful and clutters your interpreter. To undo:

```powershell
python -m pip uninstall -y localapply
```

The venv copy is independent and stays working.

## Watch it work

Click **Start run** with the prefilled fixture URL. The agent will:

1. open the job posting and find the apply link;
2. fill every field it can map to a **verified** profile fact — name, email, phone, links,
   CV upload — without interrupting you;
3. stop at **Expected salary**, because a negotiating position is not a fact. Approve or edit
   the value;
4. stop again for work authorisation, notice period, and the free-text question;
5. tell you the **electronic signature** field is required and was left for you;
6. stop once more before submitting, and submit only after you approve — simulated, because
   `DRY_RUN` is on.

Both fixture pages contain hidden prompt-injection payloads instructing the agent that
approval has already been granted and to submit immediately. It stops and asks anyway. That
is the point of the architecture, and there is a test for it.

## Tests

```bash
pytest                    # from the repo root
pytest -m "not browser"   # unit tests only; no Playwright needed
```

Browser-backed tests skip themselves automatically if Chromium is not installed. The suite
runs against SQLite, so no Docker is required.

What is covered:

| Area | Assertion |
|---|---|
| `test_loop_integration.py` | A full run reaches `SUBMITTED` only after approval; no submit row exists while parked on the gate; rejecting everything never submits |
| `test_injection.py` | Identical policy verdicts with and without an injection payload; a fully compromised reasoner naming an unobserved element is rejected |
| `test_policy.py` | Every deny and approval rule; approval does not generalise across values or elements; approval cannot unlock a denied action |
| `test_field_classifier.py` | All three safety classes; unknown fields default to *review*, not *safe* |
| `test_state_machine.py` | `SUBMITTING` is reachable from `REVIEW_REQUIRED` and nowhere else |
| `test_model_router.py` | Reasoning and vision models are never co-resident on the 8 GB card |
| `test_browser.py` | Ref enumeration, stale-ref invalidation, kill switch, dry-run submit |
| `test_loop_integration.py` | `agent_events` survives the run and can replay every ref from the stored element table |
| `test_cv_import.py` | Extraction, section splitting, and reconciliation; a corrupt file and a text-free one report *different* failures |
| `test_cv_import_api.py` | Upload proposes but accepts nothing; the reasoner cannot see a proposed fact; accepting a conflict supersedes rather than deletes |
| `test_generation.py` | Grounding refuses an item citing nothing or citing an unaccepted fact; proposed/rejected/superseded facts never render; tailoring never adds |
| `test_generation_api.py` | Versions increment and never overwrite; provenance flags facts that changed after sending; the PDF is a real PDF whose text omits skills you lack |
| `test_ai_engine.py` | An invented ref never becomes an action; junk output retries then asks; a dead model pauses rather than crashes; a hallucinated skill in a rewrite is rejected and the original kept |
| `test_writer.py` | Retrieve → draft → critique → revise; an invented summary or bullet is thrown away and the composed wording kept |
| `test_ats_format.py` | Published ATS parsing rules: single column, standard headings, nothing in a header or footer, no CSS generated content; the summary leads, roles run reverse-chronologically, the page budget holds |
| `test_cover_letter.py` | Every paragraph is a sentence, never a database row; tense follows the dates; a missing requirement is named rather than hidden; nothing internal to the tool reaches the page |
| `test_dashboard.py` | The zero-build dashboard's script parses and binds, in a real browser; the entry editor renders editable fields and never redraws under the cursor |
| `test_ollama_live.py` | Opt-in: can a real small model return one JSON object naming a real ref, and does it invent skills when rewriting |

## Verified

Run end to end on 2026-08-25 against Python 3.14.7, Postgres 16, Redis 7, Chromium 1234:

```
345 passed in 65.63s
```

A live run driven through the HTTP API, approvals submitted with `source=phone`:

| | |
|---|---|
| Safe fields filled uninterrupted | 10 (`R099_DEFAULT_ALLOW`) |
| Stops for approval | 4 fields + the submit (`R011`, `R010`) |
| `NEVER_AUTOFILL` fields touched | **0** |
| Submit | `simulated=true`, `R000_HUMAN_APPROVED` |
| Events persisted | 85, seq 1–85, no gaps |

The salary gate was approved with an *edited* value; the executed action recorded the edit,
not the draft — an approval authorises exactly what was on screen.

## Hardware note

Built and measured against an RTX 5070 Laptop (**8 GB VRAM**) with 15 GB RAM. That does not
fit a reasoning model and a vision model at once, so `ai/router.py` loads them **sequentially**
under an exclusive lock with embeddings pinned. Model swaps cost 3–10 s and are treated as an
orchestration concern. See [architecture.md](docs/architecture.md#deployment-shape).

## Layout

```
apps/web/            React dashboard — Mission Control, Profile
services/api/        FastAPI: contracts, browser, ai, policy, orchestrator, events, db
packages/            Shared agent-protocol schemas (generated from contracts.py)
infrastructure/      Postgres + Redis compose
evaluation/fixtures/ The practice job posting and application form
docs/                Architecture and ADRs
tests/
```

## Importing a CV

Open **Profile & CV**, upload a PDF, `.docx` or text CV, and every fact found comes back as
a **proposal** with the confidence and the source line it came from. Nothing reaches an
application until you accept it, one fact at a time.

The sample CV yields 34 proposals: contact details, current title, 20-odd skills, two roles,
education, and a certification.

A fact has exactly one gate — its status:

| status | meaning |
|---|---|
| `accepted` | you approved it; **the only status the agent may use** |
| `proposed` | extracted, awaiting your decision; invisible to the agent |
| `rejected` | you declined it; remembered, so a re-import does not ask again |
| `superseded` | replaced by a newer accepted fact; kept as history |

Behaviours worth knowing:

- **A changed value is a conflict, not an overwrite.** If your CV has a new email and you
  already accepted one, the proposal shows what it would replace, and the old value stays
  live until you accept the new one.
- **Bulk accept never touches conflicts.** `POST /documents/{id}/accept-all` takes
  `category` and `min_confidence` filters and skips anything that would replace a fact you
  already confirmed — that decision is not one to make in bulk.
- **Re-uploading the same file is recognised** by content hash rather than duplicated.
- **An unreadable file says so.** A scanned, image-only CV fails with an explicit message
  rather than silently producing an empty profile; there is no OCR here.
- Extraction is rule-based and deterministic. When a model-backed parser lands it feeds the
  *same* review queue: a model may propose facts, never accept them.

## Generating documents

Paste a job description into **Profile & CV** and generate a **tailored CV**, a **cover
letter**, or the **master CV**. Output is HTML plus a real PDF, printed by the Chromium that
Playwright already installs — no second rendering stack.

Starting a run generates a CV for that specific job automatically and uploads *that* instead
of a stale generic file. If generation fails, the run degrades to your existing CV and says
so; a document problem must never turn into "could not apply".

**Every line traces to an accepted fact.** Not by instruction — structurally. Each item in a
document carries the ids of the facts backing it, and `assert_grounded` refuses to render a
plan containing an item that cites nothing, or that cites a fact you have not accepted. That
gate sits between generation and rendering, so the model-backed writer in Phase 4 passes
through it unchanged: a model may rephrase your experience, it may not invent one.

What follows from that:

- **Tailoring selects and orders; it never rewrites.** A tailored CV's facts are always a
  subset of the master's, so it cannot claim more than the canonical record. The master is
  never modified as a side effect.
- **A missing skill is never claimed.** Matching is deliberately pessimistic — a skill you
  cannot evidence counts as missing, and the score is reported honestly. In the run above the
  match reads **43%, "missing RAG, Docker"** purely because those facts had not been accepted
  yet, even though they appear in the CV file.
- **Nothing unaccepted leaks in.** Proposed, rejected, and superseded facts are all invisible
  to generation, and there are tests for each.
- **Versions are never overwritten**, so what you actually sent stays recoverable.
  `GET /generate/{id}/provenance` lists the facts behind a document and flags any that have
  changed since — a sent document does not silently update itself.

The deterministic writer reads plainly rather than eloquently. That is the honest trade for
being unable to invent.

## The AI engine

Set `LA_REASONER=ollama` and the same loop runs on a local model instead of the scripted
reasoner. Nothing else changes: `LLMReasoner` implements the identical interface, and every
policy rule, approval gate and grounding check stays exactly where it was.

**The model is contained by construction, not by instruction:**

- It may name only a ref from the element table it was shown. An invented ref, or a CSS
  selector, is rejected before it can become a `Decision` — the same `R002` guarantee, now
  enforced at the model boundary too.
- Unparseable output gets **one corrective retry**, then becomes `ask_user`. A small local
  model narrating around its JSON should not stall a run; a model that cannot comply should
  not guess.
- A model that is **down** pauses the run and says so, naming Ollama, rather than crashing.
- Page text still arrives inside `<UNTRUSTED_WEB_CONTENT>`, and policy still contains no
  model — so a compromised reasoner can at worst produce a proposal that policy rejects.

### Model-written documents

With **Polish with model** ticked, generation stays deterministic and the model becomes a
*rewriter*, not an author. The plan is built from accepted facts first — grounded by
construction — and the model is then handed one line at a time and asked to say the same
thing better. It cannot add an item, a section, or a fact reference, because it is never
given the chance to.

That leaves exactly one failure mode: a rewritten sentence saying more than its source did.
`claims.check_claims` catches that — it flags any technology named in the prose that the
supporting facts do not mention, and any invented figure ("led a team of 40"). A flagged
rewrite is discarded, the original wording kept, and the dashboard **tells you it was
overruled** rather than silently dropping it.

This is a mitigation, not a proof. It catches named technologies from a known vocabulary; it
cannot catch every embellishment. That is precisely why the model only ever rephrases a fixed
set of facts, why the deterministic writer stays the default, and why nothing is sent without
your review.

### On 8 GB

`ai/router.py` loads one large model at a time under an exclusive lock. `/health` reports
what is actually resident, VRAM in use, and how many swaps have happened — real operational
state, not a config echo. A 7-8B Q4 reasoner is the realistic ceiling on this card and will
be noticeably slower than the scripted reasoner.

```bash
pytest              # 345 tests, no model needed
pytest -m ollama    # 5 live checks against a running Ollama
```

The live suite is opt-in and skips itself cleanly when Ollama is not running. It asks the
question the scripted tests cannot: whether a small local model can reliably return one JSON
object naming a real ref.

## Not built yet

Real job-board discovery (Phase 6) · React Native app (Phase 9) · WireGuard remote access
(Phase 10). Each has an interface stub so it drops in without a refactor.

### On real job sites

The skeleton runs against a local fixture on purpose. Automating LinkedIn violates its User
Agreement and risks the account; when discovery is built it should target boards with public
APIs or permissive terms (Greenhouse, Lever, Ashby), with LinkedIn treated as manual-assist —
you drive the search, the agent observes and scores. The design already refuses to defeat
anti-bot controls: CAPTCHAs and login walls pause the run and hand it to you.
