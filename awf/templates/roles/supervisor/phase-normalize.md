# Phase: normalize

## Your task
Normalize role.md files before writing any TODO. This is a **mandatory gate**:
until you call `awf_confirm_normalized`, the phase stays `normalize`
(`awf_current_step` keeps returning this prompt). The dispatch itself is not
blocked mechanically — skipping costs you a wasted pipeline cycle.

## 3-part checklist

### 1. Adaptation to iteration type
Read each `.agentic/roles/<role>.md`. Does the role fit the character?
- Analysis-only? Remove development instructions.
- Development? Ensure coding instructions present.
- Add `## Iteration adaptation` section with specifics for THIS run.

### 2. Zone overlaps
Run `awf_analyze_roles(project_dir)` — detects overlapping zones.
Resolve: add disambiguation section to each overlapping role.md.

### 3. Handoff contracts
For each role, verify it knows what it receives and produces:
```markdown
## Handoff contract
**Receives:** <what previous role produces>
**Produces:** <what this role delivers>
```

## After checklist is done
Call `awf_confirm_normalized(project_dir=<path>)`.

This advances to **brief** phase. Until the call, the phase stays `normalize`
— nothing stops a weak model mechanically, so do not skip.

## Do NOT
- Do NOT skip this step. Missing normalization = wasted iterations.
- Do NOT call `awf_dispatch_todo` — you're not in brief phase yet.
- Do NOT start pipeline — normalization first, always.
