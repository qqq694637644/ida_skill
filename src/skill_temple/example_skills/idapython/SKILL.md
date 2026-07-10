---
name: idapython
description: Use for IDAPython scripting, IDA Pro analysis, Hex-Rays decompilation, functions, xrefs, and database automation.
---

# IDAPython Example Skill

This packaged example demonstrates the Codex-style skill contract used by the GPT Action gateway: `SKILL.md` is the only required entrypoint, and task-specific resources are read progressively.

## Workflow

1. Use `listIdaInstances` and `getIdaDatabaseInfo` when live database identity matters.
2. Prefer `listIdaFunctions`, `decompileIdaFunction`, and `getIdaXrefs` for direct reads.
3. Use `executeIdapython` for custom analysis or mutations, then inspect `status`, `stdout`, `stderr`, `result`, and `error`.

## Progressive disclosure

- Read `docs/idautils.md` for function, string, and xref iterators.
- Read `docs/ida_hexrays.md` for decompilation and ctree visitors.
- Use `readSkillContent` with this skill's explicit `skill_id` and exact relative path.
- Use `searchSkillDocs` only when no exact referenced path covers the task.

## IDAPython rules

- Prefer modern `ida_*` modules and include imports.
- Call `ida_auto.auto_wait()` before relying on analysis results.
- Assume `ea_t` may contain 64-bit addresses.
- Handle unavailable Hex-Rays or decompilation failure explicitly.
