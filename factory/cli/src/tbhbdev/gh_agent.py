"""Where the agent's own gh config lives, relative to its home directory."""

# The gh config of the agent's GitHub identity. It holds no credential; gitleaks
# flags any path into .config.
CONFIG_DIR = ".config/gh-agent"  # gitleaks:allow
