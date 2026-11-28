"""The coordinator's file contract with run-host, relative to the run directory."""

DIRECTORY = "tbhbdev/agent-runs"
REQUEST = ".request.json"
PROMPT = ".prompt"
SCHEMA = ".schema.json"
PGID = ".pgid"
LAUNCHED = ".launched"
PHASE = ".phase"
OUT = ".out"
FINAL = ".out.final"
LOG = ".log"
BUILD = ".build"
CLI = ".cli"
CLI_ARCHIVE = ".cli.tar"
BRANCH = ".branch"
PR = ".pr"
REVIEW = ".review"
BASE = ".base"
HEAD = ".head"
RC = ".rc"
KEEP = ".keep"
RC_PENDING = ".rc.pending"
RUN_LOCK = ".run.lock"
FALLBACK = ".rc.fallback"
OWNER = ".owner"
AUDIT = ".audit.json"
SUFFIXES = (
    REQUEST,
    PROMPT,
    SCHEMA,
    PGID,
    LAUNCHED,
    PHASE,
    OUT,
    FINAL,
    LOG,
    BUILD,
    CLI_ARCHIVE,
    CLI,
    BRANCH,
    PR,
    REVIEW,
    BASE,
    HEAD,
    KEEP,
    RC_PENDING,
    FALLBACK,
    RC,
    AUDIT,
    RUN_LOCK,
    OWNER,  # Keep ownership until every other file is archived.
)
