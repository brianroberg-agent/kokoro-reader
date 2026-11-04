# narration-agent: the session service, which runs one locked-down Claude Code session
# at a time for the runner's handoff requests. It holds only the Claude login. Built
# from the repo root by deploy/compose.yaml. agentbox's entrypoint applies the egress
# firewall as root, then runs the command as node.
FROM agentbox:latest

# The compose file's AGENTBOX_GITHUB=off and AGENTBOX_BASE=off need an agentbox image
# that knows them: an older one ignores AGENTBOX_BASE=off without a word and leaves
# its base hosts reachable. Stop the build instead.
RUN grep -q AGENTBOX_GITHUB /usr/local/bin/init-firewall.sh && grep -q AGENTBOX_BASE /usr/local/bin/init-firewall.sh \
  || { echo "agentbox:latest predates AGENTBOX_BASE (agentbox PR #14); rebuild it from agentbox main" >&2; exit 1; }

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

# No setuid or setgid program and no file capability is left in the image. agentbox drops
# root with gosu, which isn't setuid, so nothing here needs one. Running one is how a
# process in the container, as node, would get root or a capability back; no-new-privileges
# stops that too, on a host whose Docker allows it (docs/pipeline.md, "Docker from the snap
# package"). The last step, so nothing installed after it brings one back.
RUN find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod ug-s {} + \
  && command -v getcap > /dev/null \
  && left="$(find / -xdev -type f \( -perm -4000 -o -perm -2000 \); find / -xdev -type f -exec getcap {} +)" \
  && test -z "$left" \
  || { printf '%s\n%s\n' "a setuid or setgid program or a file capability is left in the image (or getcap, to look for one, is missing):" "${left:-}" >&2; exit 1; }

CMD ["python3", "/opt/narration/session_service.py"]
