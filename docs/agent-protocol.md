# Agent protocol

Agents do not exchange free-form text. Every hop in the loop is a typed message defined in
`services/api/localapply/contracts.py`, which makes each layer independently testable and
every run replayable from its event log.

## One iteration

```
Observer                Reasoner              Policy                Executor
   │                       │                     │                      │
   ├── Observation ───────►│                     │                      │
   │                       ├── Decision ────────►│                      │
   │                       │                     ├── PolicyVerdict ────►│
   │                       │                     │                      ├── ActionResult
   │◄─────────────────────────────────── (observe again) ───────────────┘
```

## Observation

```json
{
  "observation_id": "…", "run_id": "…",
  "url": "http://localhost:8000/fixtures/apply.html",
  "title": "Apply — AI Engineer at Northwind Analytics",
  "page_kind": "application_form",
  "screenshot_id": "…",
  "elements": [
    { "ref": "e1", "role": "textbox", "name": "First name",
      "input_type": "text", "required": true, "enabled": true, "visible": true }
  ],
  "untrusted_text": "…visible page text…"
}
```

`elements` is the reasoner's **entire vocabulary** for the page. `untrusted_text` is
third-party content and is fenced in `<UNTRUSTED_WEB_CONTENT>` before it reaches a model.

## Decision

```json
{ "action": "type", "target_ref": "e1", "value": "Haidar",
  "confidence": 0.95, "reason": "first name maps to a verified profile fact" }
```

A proposal with no effect of its own. `target_ref` is constrained to `^e\d+$`, so a selector
or a coordinate cannot even be expressed.

Actions: `navigate · click · type · scroll · select · upload · wait · extract · screenshot ·
submit · ask_user · finish`. `submit` is separate from `click` so it can be gated in exactly
one place.

## PolicyVerdict

```json
{ "outcome": "require_approval", "rule_id": "R011_REVIEW_REQUIRED_FIELD",
  "reason": "Field 'Expected salary' is salary; confirm the value before it is entered.",
  "field_class": "review_required" }
```

Produced by plain code with no model in it. Rules, in evaluation order:

| Rule | Outcome | Meaning |
|---|---|---|
| `R001_KILL_SWITCH` | deny | Automation is stopped |
| `R002_UNKNOWN_REF` / `R002_MISSING_TARGET` | deny | Ref was not in this observation |
| `R003_CAPABILITY` | deny | Agent has no capability for this action |
| `R004_ACTION_BUDGET` | deny | Run exceeded its action ceiling |
| `R005_NEVER_AUTOFILL` | deny | Signature, government ID, demographic, credential |
| `R006_ELEMENT_DISABLED` / `R006_ELEMENT_HIDDEN` | deny | Element is not usable |
| `R000_HUMAN_APPROVED` | allow | This exact action was approved by a human |
| `R010_SUBMIT_ALWAYS_GATED` | approve | Every submit, always |
| `R011_REVIEW_REQUIRED_FIELD` | approve | Salary, sponsorship, availability, narrative |
| `R012_LOW_CONFIDENCE` | approve | Mutating action below the confidence threshold |
| `R013_INTERVENTION_PAGE` | approve | CAPTCHA or login — handed to the human |
| `R099_DEFAULT_ALLOW` | allow | No rule objected |

Deny rules are evaluated **before** the human-approval check, so a person cannot approve
their way past a categorical prohibition.

## ActionResult

```json
{ "action": "submit", "success": true, "new_page_state": "…/apply.html",
  "duration_ms": 12, "simulated": true }
```

`simulated: true` means `DRY_RUN` suppressed the real side effect. The decision, the
approval, and the log entry are all real; only the click was withheld.

## AgentEvent

Every message above is wrapped in an `AgentEvent` and appended to the run log — the same
shape on the SSE wire and in `agent_events`. `seq` is monotonic per run, so a dashboard can
reconnect and resume from where it left off.

Observation events carry the **full element table**, which is what keeps a ref named in a
later event resolvable when replaying the run weeks afterwards.
