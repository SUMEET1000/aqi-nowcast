"""Credential loading and console encoding. Stdlib only, imports nothing local.

Separate from cpcb_api.py because db.py needs require_env and nothing else from
it. Keeping them together made the Postgres module import the HTTP client, so a
syntax error in the CPCB code broke init_db.py, which has no business touching
the network. This is the leaf both of them depend on instead.

Nothing here may import another project module — that is the whole point.
"""

import os
import sys

# Windows consoles default to cp1252, which renders the '§' and '—' in these
# scripts' output as '?'. Gate output is meant to be readable (and screenshot-
# able) evidence, so force UTF-8. Done here because every entry point reaches
# this module transitively; the alternative is repeating it in six files.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def load_env(name: str) -> str | None:
    """Read a value from the environment, falling back to .env. Never printed.

    Splits on the first '=' only, so values containing '=' survive intact — a
    Postgres URL ends in '?sslmode=require' and would otherwise be truncated.
    """
    value = os.environ.get(name)
    if value:
        return value

    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return None

    prefix = f"{name}="
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(prefix):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def require_env(name: str, hint: str) -> str:
    """load_env, but a missing value is a hard stop rather than a default (§0.5)."""
    value = load_env(name)
    if not value:
        sys.exit(f"{name} is not set. Put it in .env (see .env.example).\n{hint}")
    return value
