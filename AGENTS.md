# Contributor Agent Guide

This repository ships a reusable skill and a zero-dependency Python runner for delegating work to installed Code CLIs.

## Non-negotiable boundaries

- Never add automatic CLI installation, download-on-demand wrappers, or silent substitution of a requested product.
- Never add `shell=True`, shell pipelines, implicit permission bypasses, or credentials in prompts/arguments.
- Background execution remains the default; tmux is optional and deliberate.
- CLI output is evidence, not acceptance. Review the actual Git changes independently.
- In PR delivery mode, the implementation CLI may open/update the feature PR but never merge, push the default branch, deploy after merge, or claim Engineering Done/Accepted/Released.
- Do not turn status checks into a continuous watcher. GitHub/CI review remains one-shot and resumable.
- A `merge_ready` checkpoint must continue to bind the reviewed local commit to the exact PR head and a clean worktree.

## Change locations

- Skill routing and invariants: `SKILL.md`.
- Session behavior: `scripts/code-cli-session.py`.
- Built-in adapters: `scripts/cli-profiles.json` and `references/cli-profiles.md`.
- PR workflow semantics: `references/ai-native-delivery.md`.
- Observable behavior tests: `tests/`.

## Validation

Run before committing:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 -m json.tool scripts/cli-profiles.json >/dev/null
bash -n scripts/scan-code-clis.sh scripts/start-code-cli-session.sh
```

Adapter changes must include current `--help` evidence in the PR and a harmless isolated test. A profile stays `detect-only` or `provisional` until its non-interactive invocation is verified.
