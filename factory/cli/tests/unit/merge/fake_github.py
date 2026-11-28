# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Stand-ins for the executables used by the merge command."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tbhbdev.merge.command import main

FIXTURE = (
    Path(__file__).parents[2]
    / "testdata"
    / "alpha"
    / "pull_request"
    / "passing-draft.md"
)


TARGET = "tbhb-dev/public-target"


APPROVED_HEAD = "a" * 40


OTHER_HEAD = "b" * 40


REVIEWS_APP = "tbhb-dev-agent-reviews"


REVIEWS = [
    {
        "id": f"review-{harness}",
        "author": {"login": REVIEWS_APP, "__typename": "Bot"},
        "body": f"Reviewed.\n\nGenerated-by: LLM harness={harness};model=m\n",
        "state": "APPROVED",
        "commit": {"oid": APPROVED_HEAD},
    }
    for harness in ("claude", "gemini")
]


def commit(head: object = APPROVED_HEAD, *emails: str) -> dict[str, object]:
    return {
        "oid": head,
        "messageHeadline": "fix: tidy",
        "messageBody": "",
        "authors": [{"email": email} for email in emails],
    }


GH = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "gh.spec")))
repo = sys.argv[1] == "repo"
settings = repo and "mergeCommitAllowed" in sys.argv
# A read with no repository argument names the working directory's repository.
if settings:
    kind = "settings"
elif repo:
    kind = "cwd" if sys.argv[2].startswith("-") else "repo"
elif sys.argv[1:3] == ["api", "graphql"] and "reviews(first:" in " ".join(sys.argv):
    kind = "reviews"
elif sys.argv[1] == "api":
    kind = "rules" if sys.argv[2] != "graphql" else "queue"
else:
    kind = sys.argv[2] if sys.argv[1] == "pr" else "?"
# The rules and repo reads have their own logs.
logname = f"gh.{{kind}}.log" if kind in ("settings", "repo", "rules", "reviews") else (
    "gh.repo.log" if repo else "gh.log"
)
log_path = os.path.join(here, logname)
if kind == "queue" and "queue_before_out" in spec:
    with open(log_path) as log:
        if not any(json.loads(line)["argv"][:2] == ["pr", "merge"] for line in log):
            spec["queue_out"] = spec["queue_before_out"]
with open(log_path, "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
sys.stdout.write(spec[kind + "_out"])
sys.stderr.write(spec[kind + "_err"])
rc = spec[kind + "_rc"]
if rc < 0:
    sys.stderr.flush()
    os.kill(os.getpid(), -rc)
sys.exit(rc)
"""


GITLEAKS = """import json, os, sys
if sys.argv[1:] == ["--version"]:
    print({version!r})
    sys.exit(0)
here = os.path.dirname(os.path.abspath(__file__))
spec = os.path.join(here, "gitleaks.spec")
rc, out = json.load(open(spec)) if os.path.exists(spec) else (0, "[]")
with open(os.path.join(here, "gitleaks.log"), "a") as log:
    config = open(sys.argv[sys.argv.index("--config") + 1]).read()
    entry = {{
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
        "argv": sys.argv[1:],
        "config": config,
    }}
    log.write(json.dumps(entry) + "\\n")
sys.stdout.write(out)
sys.exit(rc)
"""


COG = """import sys
text = sys.stdin.read()
if "BAD" in text:
    sys.stderr.write("not conventional")
    sys.exit(1)
"""


@dataclass
class Tools:
    """Fake gh and cog on an isolated PATH."""

    bin: Path

    def gh(self, **spec: object) -> None:
        full: dict[str, object] = {
            "view_rc": 0,
            "view_out": "",
            "view_err": "",
            "merge_rc": 0,
            "merge_out": "merged\n",
            "merge_err": "",
            "repo_rc": 0,
            "repo_out": "PUBLIC\\n",
            "repo_err": "",
            "settings_rc": 0,
            "settings_out": '{"mergeCommitAllowed": true}',
            "settings_err": "",
            "rules_rc": 0,
            "rules_out": '[[{"type": "pull_request"}]]',
            "rules_err": "",
            "reviews_rc": 0,
            "reviews_out": "",
            "reviews_err": "",
            "queue_rc": 0,
            "queue_out": "",
            "queue_err": "",
            "cwd_rc": 0,
            "cwd_out": "PRIVATE\\n",
            "cwd_err": "",
        }
        (self.bin / "gh.spec").write_text(json.dumps(full | spec))

    def view(self, **fields: object) -> None:
        head = fields.get("headRefOid", APPROVED_HEAD)
        data: dict[str, object] = {
            "title": "feat(cli): add the stamp subcommand",
            "body": FIXTURE.read_text().split("\n# ", 1)[1].split("\n", 1)[1],
            "headRefOid": APPROVED_HEAD,
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"name": "conclusion", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviewDecision": "APPROVED",
            "commits": [commit(head)],
            "url": f"https://github.com/{TARGET}/pull/12",
        }
        data.update(fields)
        pull = {
            "headRefOid": head,
            "reviewDecision": data["reviewDecision"],
            "reviews": {
                "pageInfo": {"hasNextPage": False},
                "nodes": data.pop("reviews", REVIEWS),
            },
        }
        self.gh(
            view_out=json.dumps(data),
            reviews_out=json.dumps({"data": {"repository": {"pullRequest": pull}}}),
        )

    def scanned(self) -> list[dict[str, object]]:
        log = self.bin / "gitleaks.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def calls(self) -> list[dict[str, object]]:
        log = self.bin / "gh.log"
        return [json.loads(line) for line in log.read_text().splitlines()]


def amend(tools: Tools, **spec: object) -> None:
    """Change the fake gh's answers, keeping the rest."""
    path = tools.bin / "gh.spec"
    path.write_text(json.dumps(json.loads(path.read_text()) | spec))


def repo_reads(tools: Tools) -> list[dict[str, object]]:
    log = tools.bin / "gh.repo.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def read_back(message: str) -> str:
    done = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def queue_data(*, removal: dict[str, object] | None = None, **fields: object) -> str:
    nodes = [] if removal is None else [removal]
    request = {"timelineItems": {"nodes": nodes}, **fields}
    return json.dumps({"data": {"repository": {"pullRequest": request}}})


def queue_rules(method: str) -> str:
    return json.dumps(
        [[{"type": "merge_queue", "parameters": {"merge_method": method}}]]
    )


def run_main(*argv: str) -> int | str | None:
    try:
        return main(list(argv))
    except SystemExit as exc:
        return exc.code
