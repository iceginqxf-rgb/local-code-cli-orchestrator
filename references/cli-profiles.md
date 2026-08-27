# CLI profiles

The executable registry is [scripts/cli-profiles.json](../scripts/cli-profiles.json). The runner merges three layers in order:

1. The built-in registry shipped with this skill.
2. The optional user registry at `~/.codex/local-code-cli-orchestrator/cli-profiles.json`.
3. An optional registry passed with the global `--registry /absolute/path.json` argument.

Later layers replace profiles with the same ID. This keeps local adaptations outside the installed skill.

## Built-in coverage

Ready non-interactive profiles:

- `codex` — OpenAI Codex CLI (`codex exec`).
- `claude` — Claude Code print mode.
- `copilot` and `gh-copilot` — standalone GitHub Copilot CLI or an already-downloaded `gh` payload.
- `agy` — Google Antigravity print mode.
- `grok` / `grok-build` — Grok Build single-turn prompt-file mode.
- `droid` / `factory-droid` — Factory Droid `exec` mode.
- `kimi` / `kimi-code` — Kimi Code prompt mode.
- `pi` — Pi Coding Agent print mode.
- `opencode` — OpenCode `run` mode.
- `gemini` — Google Gemini CLI headless mode.
- `qwen` / `qwen-code` — Qwen Code headless mode.
- `aider` — Aider message-file mode, with automatic commits disabled by the adapter.
- `goose` — Goose `run` instruction-file mode.

Provisional profile:

- `kiro-cli` — requires checking the installed version and passing `--allow-provisional`.

Detection-only profiles:

- `cursor-agent`, `cursor`, `amp`, `crush`, `cline`, `roo`, and `auggie`.

Detection-only means the skill will report presence but will not guess a background command. Add a reviewed user override after checking that product's current `--help` output.

## Profile format

Example user registry:

```json
{
  "schema_version": 1,
  "profiles": {
    "my-agent": {
      "label": "My Code Agent",
      "aliases": ["my-code"],
      "support": "ready",
      "commands": ["my-agent"],
      "command": ["{executable}"],
      "base_args": ["run", "--format", "text"],
      "prompt": {
        "transport": "file",
        "args": ["--prompt-file", "{prompt_file}"]
      },
      "model_args": ["--model", "{model}"],
      "effort_args": ["--effort", "{effort}"],
      "version_args": ["--version"]
    }
  }
}
```

Supported placeholders:

- `{executable}` — resolved command path.
- `{prompt}` — prompt body for argument transport.
- `{prompt_file}` — durable session prompt snapshot.
- `{result_file}` — durable result path for CLIs that can write their last message directly.
- `{cwd}` — resolved working directory.
- `{model}` and `{effort}` — supplied selector values.

Prompt transports:

- `file`: prefer when the CLI accepts a prompt file.
- `stdin`: useful for large prompts and avoids exposing the body in process listings.
- `arg`: use only when the CLI requires a prompt argument; the runner rejects argument prompts above 200 KB.

Commands execute as argument arrays with `shell=False`. Do not put shell pipelines, redirections, command substitutions, or quoting tricks in a profile.

## Adding or changing a profile

1. Confirm the product and command name; similarly named binaries can be unrelated.
2. Run the installed CLI's `--help` and its non-interactive subcommand help.
3. Prefer official documentation for commands not installed locally.
4. Choose a prompt-file or stdin mode where available.
5. Use plain or final-result output by default; keep streaming JSON optional for explicit diagnostics.
6. Do not add auto-install behavior or unsafe permission bypasses.
7. Test with a harmless temporary repository before marking the profile `ready`.

If the requested CLI is absent, report that fact and ask the user whether to install it. The skill does not install it automatically.
