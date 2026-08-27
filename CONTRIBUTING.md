# Contributing

Contributions are welcome, especially verified adapters, portability fixes, and stronger review safeguards.

## Before opening a PR

1. Keep the change focused and explain the behavior or risk it addresses.
2. For an adapter change, identify the exact product/version and include relevant non-interactive `--help` evidence.
3. Do not add installers, unsafe permission bypasses, shell execution, or automatic product substitution.
4. Add or update an observable behavior test.
5. Run the validation commands from [README.md](README.md#development).

Adapter support levels are intentional:

- `ready`: a non-interactive invocation is verified and safe for the background runner.
- `provisional`: plausible invocation that still requires installed-version verification.
- `detect-only`: presence can be detected, but the runner will not guess how to launch it.

Use GitHub Security Advisories instead of a public Issue for vulnerabilities.
