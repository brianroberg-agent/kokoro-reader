# narration-agent: the session service, which runs one locked-down Claude Code session
# at a time for the runner's handoff requests. It holds only the Claude login. Built
# from the repo root by deploy/compose.yaml. agentbox's entrypoint applies the egress
# firewall as root, then runs the command as node.
FROM agentbox:latest

ENV DISABLE_AUTOUPDATER=1

# The Claude Code version the sessions have been checked with, in place of the one the
# base image was built with. Installed as root, like the base image's, so its files
# belong to root rather than to node, the user the session runs as.
ARG CLAUDE_CODE_VERSION=2.1.316
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
  && test "$(claude --version | cut -d' ' -f1)" = "$CLAUDE_CODE_VERSION"

# The service imports only the standard library, so it runs on Debian's python3 with
# nothing installed.
COPY pipeline/narration_pipeline/session_service.py pipeline/narration_pipeline/agent_prompt.md /opt/narration/

# The named volume is seeded from this empty directory, so it starts owned by node.
RUN install -d -o node -g node -m 0700 /handoff

CMD ["python3", "/opt/narration/session_service.py"]
