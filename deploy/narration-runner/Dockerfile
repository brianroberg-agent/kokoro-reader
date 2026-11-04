# narration-runner: the nightly loop (python -m narration_pipeline schedule), which
# holds the Audiobookshelf key, the ntfy token and the narration runner token. Built
# from the repo root by deploy/compose.yaml. agentbox's entrypoint applies the egress
# firewall as root, then runs the command as node.
FROM agentbox:latest

# The compose file's AGENTBOX_GITHUB=off and AGENTBOX_BASE=off need an agentbox image
# that knows them: an older one ignores AGENTBOX_BASE=off without a word and leaves
# its base hosts reachable. Stop the build instead.
RUN grep -q AGENTBOX_GITHUB /usr/local/bin/init-firewall.sh && grep -q AGENTBOX_BASE /usr/local/bin/init-firewall.sh \
  || { echo "agentbox:latest predates AGENTBOX_BASE (agentbox PR #14); rebuild it from agentbox main" >&2; exit 1; }

# Python 3.13 for both projects, the version they run on on the Mac: article-assistant
# needs 3.13 or later, and the image has Debian's 3.11. uv downloads it at build time,
# here rather than under /root (mode 700, closed to node); the same settings let uv
# find it again at run time.
ENV UV_PYTHON=3.13
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

# --frozen, not --locked: the image's dependency cooldown makes a committed uv.lock look
# stale, and --locked fails (agentbox README, "uv in the image"). --no-dev leaves the
# test tools out, and --no-cache keeps uv's download cache out of the image. Built as
# root and left root's: node can read and run the code and its venvs, not change them.
# The repository is a build argument so a test build can use a branch that isn't merged
# yet (--build-arg ARTICLE_ASSISTANT_REPO=<a fork's URL>); compose takes the default.
ARG ARTICLE_ASSISTANT_REPO=https://github.com/brianroberg/article-assistant.git
ARG ARTICLE_ASSISTANT_REF=eef0d1f
RUN git clone --quiet "$ARTICLE_ASSISTANT_REPO" /opt/article-assistant \
  && git -C /opt/article-assistant -c advice.detachedHead=false checkout --quiet "$ARTICLE_ASSISTANT_REF" \
  && cd /opt/article-assistant \
  && uv sync --frozen --no-dev --no-cache

COPY pipeline/ /app/
RUN cd /app && uv sync --frozen --no-dev --no-cache

# `uv run` runs what the build installed, without syncing first. Otherwise, at every
# start, uv finds narration-pipeline "modified" (it compares ctimes, which the image's
# layers change) and rebuilds it, and it would install the dev group the build left out:
# both fetch from PyPI, which the firewall blocks, so the runner would exit and restart
# in a loop.
ENV UV_NO_SYNC=1

# What node writes: the record (and the run's lock and temporary files) in /data, the
# handoff in /handoff. The named volumes are seeded from these empty directories, so
# they start owned by node.
RUN install -d -o node -g node -m 0700 /data /handoff

WORKDIR /app
# One night and its summary, then exit: compose's restart starts the next night, and
# the firewall resolves the allowed hosts again (it resolves them once, at start).
CMD ["uv", "run", "--frozen", "python", "-m", "narration_pipeline", "schedule", "--nights", "1"]
