"""Identifier grammars shared by the registry, the token map and the headers."""

import re

# Registry ids (spec §4) — models, aliases, projects and callers. The caller
# ids in the SM token map use the same grammar so a token can never name a
# caller the registry could not have declared.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")

# X-Gateway-Job-Id (spec §3).
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
