# The pipeline on the home server

Each night the runner looks for new articles, has a Claude Code session record each one on the Mac's narration server, and publishes the finished episodes to Audiobookshelf. It runs as two containers on the home server, both built on agentbox's image and egress firewall:

- **`narration-runner`** runs `python -m narration_pipeline schedule --nights 1`. It holds the Audiobookshelf key, the ntfy token and the narration runner token. It keeps the record in the `narration-data` volume (`/data/record.sqlite`).
- **`narration-agent`** runs the session service, which takes one request at a time from the `narration-handoff` volume and runs a locked-down `claude -p` with only the six narration tools. It holds only the Claude login.

The Mac runs the narration server (`docs/narration-server.md`). The files are `deploy/compose.yaml`, the two Dockerfiles in `deploy/narration-runner/` and `deploy/narration-agent/`, and the three examples `deploy/env.example`, `deploy/runner.env.example` and `deploy/agent.env.example`.

Each source is a `[[source]]` entry in `pipeline/sources.toml`. Its `extractor` names the article-assistant extractor its site needs, and the runner refuses a name it doesn't know (`new-atlantis` is the one there is); article-assistant itself picks the extractor from each article's URL, so the key documents the source rather than choosing anything. A new site needs its extractor in article-assistant first, then its name added to `EXTRACTORS` in `pipeline/narration_pipeline/config.py`.

The runner starts the night at 01:00 New York time, keeps trying to reach the Mac until 05:00, starts no new article after 06:00, and sends the summary at 07:00 (`pipeline/sources.toml`, `[settings]`). Then it exits, and compose's `restart: unless-stopped` starts it again (see "Name lookups" below for why).

## What each container may reach

| | `narration-runner` | `narration-agent` |
|---|---|---|
| Profile | `restricted`, `AGENTBOX_GITHUB=off`, `AGENTBOX_BASE=off` | `restricted`, `AGENTBOX_GITHUB=off`, `AGENTBOX_BASE=off` |
| Hostnames (every port) | `RUNNER_ALLOW`: the source site and the ntfy host | none |
| Host:port grants | the Mac's narration port, plus `RUNNER_LAN` (Audiobookshelf by address) | the Mac's narration port |
| Also, from agentbox | `api.anthropic.com` (`code.claude.com` and `docs.claude.com` share its address) | the same |

Neither container publishes a port, and the firewall drops inbound connections. The runner hands work to the agent through the `narration-handoff` volume.

### Name lookups

agentbox's firewall resolves each allowed hostname once, when the container starts (its entrypoint runs the firewall before the command), and lets in the addresses it got then. An address a host moves to later stays blocked until the container starts again.

- **The runner** starts again after each night's 07:00 summary, so at 01:00 its lookups of the source site and the ntfy host are about 18 hours old. That is fine while the source site's CDN keeps its addresses for a day, as it typically does, and each night's restart looks them up afresh for the next.
- **The agent** looks up `api.anthropic.com` when its container starts, and runs until it is stopped. If every session fails to connect to the API, recreate it so it looks the name up again: `docker compose -f deploy/compose.yaml up -d --force-recreate narration-agent`.
- The Mac's grant is an address (`MAC_HOST`), so no lookup is involved.

## The secrets Brian makes

Each goes into one of two files on the home server, not into the repo or an image.

| Variable | File | Where it comes from |
|---|---|---|
| `NARRATION_RUNNER_TOKEN` | `runner.env` | The same value as the Mac's `~/.config/narration/runner-token` (`docs/narration-server.md`). Copy it across by hand, not through chat. |
| `AUDIOBOOKSHELF_URL`, `AUDIOBOOKSHELF_API_KEY` | `runner.env` | The server's address and an API key with upload rights to "The New Atlantis". |
| `NTFY_TOPIC_URL`, `NTFY_TOKEN` | `runner.env` | An ntfy topic named `narration` with its own access token. `NTFY_TOPIC_URL` is the topic's full URL, `https://<ntfy host>/narration`, and the runner posts to it with the token as a bearer token. |
| `CLAUDE_CODE_OAUTH_TOKEN` | `agent.env` | `claude setup-token`, run on the Mac (it works with the Max subscription). The token lasts about a year, per agentbox's notes. |

**The Claude login expires.** Record the day the token was made here, and set a reminder for 11 months later to make a new one:

- Claude token created: _(date)_. Reminder set for: _(date)_.

If it lapses anyway, or the token in `agent.env` is wrong, the first session of the night can't authenticate. The runner then pushes "Claude login failed", with "Claude Code could not authenticate (CLAUDE_CODE_OAUTH_TOKEN missing, expired or invalid)", and stops the night. The article goes back to `failed` with that as its `last_error` and without a failed night counted against it, so it comes first the next night, and the push comes again each night until the token is fixed.

To renew it, run `claude setup-token` again, put the new value in `/srv/narration/agent.env`, and recreate the agent so it reads the file again (a plain `restart` keeps the old environment): `docker compose -f deploy/compose.yaml up -d --force-recreate narration-agent`. Then update the date above.

**If Claude Code can't reach the API** for another reason (it reports the request as overloaded, rate-limited or failed) before the session's first narration tool call, the runner pushes "Claude API unreachable" with the session's error and stops the night the same way: the article goes back to `failed` without a failed night, and comes first the next night. If that push comes every night, recreate the agent (see "Name lookups" above). Once the session has called a tool, the article's text is in it, so a failed API request from then on counts against the article like any other failure (an article that made every request fail would otherwise stop every night).

## Deploy

On the home server, from scratch:

1. **Build agentbox's image** from agentbox `main` (once the `AGENTBOX_BASE` change has merged: both containers set `AGENTBOX_BASE=off`, which needs it):

   ```bash
   git clone https://github.com/brianroberg/agentbox.git ~/agentbox
   docker build -t agentbox:latest ~/agentbox/image/
   ```

   Rebuild it, and then these two images, about monthly: that is how the toolchain gets updates. Claude Code inside `narration-agent` stays at `CLAUDE_CODE_VERSION` in `deploy/narration-agent/Dockerfile` whatever the base has; change that line to move it.

2. **Get the repo:** `git clone https://github.com/brianroberg/kokoro-reader.git ~/kokoro-reader && cd ~/kokoro-reader`. The commands below run from the repo root.

3. **Write the secrets**, owned by the account that runs `docker compose`, mode 600:

   ```bash
   sudo install -d -m 700 -o "$USER" /srv/narration
   install -m 600 deploy/runner.env.example /srv/narration/runner.env
   install -m 600 deploy/agent.env.example /srv/narration/agent.env
   # then fill in the values with an editor
   ```

   compose stops with "env file /srv/narration/runner.env not found" if a file is missing.

4. **Write `deploy/.env`** from `deploy/env.example` (`cp deploy/env.example deploy/.env`). It is git-ignored. Fill in:
   - `MAC_HOST`: the Mac's tailnet address, `tailscale ip -4` on the Mac. Use the address, not the name: the firewall makes its grant when the container starts, and a tailnet name may not resolve inside a container. The narration server accepts the address in the Host header, since the plist's `NARRATION_ALLOWED_HOSTS` lists `TAILNET_IP:8770`.
   - `MAC_PORT`: leave at `8770` unless the plist's `NARRATION_PORT` differs.
   - `RUNNER_ALLOW`: `www.thenewatlantis.com,<the ntfy host>`, plus Audiobookshelf's hostname if `AUDIOBOOKSHELF_URL` uses one. A hostname here is required: the runner doesn't start if it can't be resolved.
   - `RUNNER_LAN`: Audiobookshelf's `address:port` if `AUDIOBOOKSHELF_URL` uses an address; otherwise empty.

   compose stops with "set MAC_HOST in deploy/.env" (or `RUNNER_ALLOW`) when one of them is unset.

5. **Check that the Mac answers** from the home server: `curl -s http://<MAC_HOST>:8770/health` gives `{"ok":true}`.

6. **Build and start:**

   ```bash
   docker compose -f deploy/compose.yaml up -d --build
   ```

7. **Check the start:**

   ```bash
   docker compose -f deploy/compose.yaml ps
   docker compose -f deploy/compose.yaml logs narration-agent narration-runner
   ```

   Both logs show agentbox's firewall lines ending in `profile=restricted applied` and `dropping privileges to node`. The agent then logs `serving /handoff for http://<MAC_HOST>:8770/agent/mcp, prompt /opt/narration/agent_prompt.md`. The runner then logs `next run at <time>` (01:00 New York time, as an ISO time) and waits. If a container keeps restarting, its log says why: for example a hostname in `RUNNER_ALLOW` that doesn't resolve, or a missing variable in `runner.env` (the runner names it, never its value).

   Then check that the containers, not only the host, reach the Mac over the tailnet: `docker compose -f deploy/compose.yaml exec -u node narration-runner curl -s http://<MAC_HOST>:8770/health` gives `{"ok":true}`.

Before the first night, do a dry run and then a real run of one article (Operate, below), as Plan 3's Task 11 says.

`docker compose config` prints the merged configuration with the secrets from both files in it: don't paste its output anywhere.

## Operate

Every `exec` below has `-u node`. `exec` doesn't go through agentbox's entrypoint, so without it the command runs as root, and files it leaves in `/data` or `/handoff` would belong to root, where the node processes may then fail to write. `run --rm` does go through the entrypoint (firewall, then node), so it needs no `-u`.

**Status:** counts by status and the ten rows updated last.

```bash
docker compose -f deploy/compose.yaml exec -u node narration-runner uv run --frozen python -m narration_pipeline status
```

**A dry run of one article.** It fetches, records and checks the article and finalizes the work item, then reports what it would have done (`would publish`, with the notes), but publishes nothing, sends no push and leaves the row `queued`. The URL must already be in the record from a night's discovery; otherwise the run stops with `not discovered: <URL>`.

```bash
docker compose -f deploy/compose.yaml run --rm narration-runner \
  uv run --frozen python -m narration_pipeline run --dry-run --only <article URL>
```

Check the Mac first (`docker compose -f deploy/compose.yaml exec -u node narration-runner curl -s http://<MAC_HOST>:8770/health`): a run that can't reach the Mac keeps asking every five minutes until 05:00 (the next morning, for a run started in the daytime). A real run of one article is the same command without `--dry-run`. Another run in progress makes it stop with `another run is in progress`.

**Reading the record.** The image has no `sqlite3` command; Python's module reads it, read-only:

```bash
docker compose -f deploy/compose.yaml exec -u node narration-runner python3 -c "
import sqlite3
db = sqlite3.connect('file:/data/record.sqlite?mode=ro', uri=True)
for row in db.execute('SELECT updated_at, status, failed_nights, rounds, claude_cost_usd, last_error, title '
                      'FROM articles ORDER BY updated_at DESC LIMIT 20'):
    print(*row, sep='  |  ')"
```

The columns are in `pipeline/narration_pipeline/record.py`. The ones worth reading: `status` (`baseline`, `queued`, `in_progress`, `published`, `published_with_notes`, `held`, `failed`), `failed_nights`, `last_error`, `remaining_issues` (the notes published with the episode, or the reasons for a hold), `episode_id`, and the costs (`claude_cost_usd`, `claude_tokens`, `gemini_cost_usd`, `run_seconds`).

**Logs:** `docker compose -f deploy/compose.yaml logs --since 24h narration-runner` (and `narration-agent`).

**"Narration session service not running".** The agent's session service touches `/handoff/heartbeat` every 2 seconds, also while a session runs. Before each article the runner reads it: when it is missing or more than 60 seconds old, the runner sends this push and starts no article that night; an article whose session got no result while the heartbeat stopped goes back to `failed` without a failed night. Look at `docker compose -f deploy/compose.yaml ps` and the agent's log, then start it again: `docker compose -f deploy/compose.yaml up -d narration-agent`. When the runner stops waiting for a result, it withdraws the request if the service hasn't taken it yet, and the service answers a request older than its timeout plus 300 seconds with "stale request" instead of starting a session.

**Updating** after a `git pull`: `docker compose -f deploy/compose.yaml up -d --build`. Do it in the daytime: a runner restarted mid-night leaves its article `in_progress`, and the next night recovers it.

## Rollback

```bash
docker compose -f deploy/compose.yaml down
```

This stops and removes both containers. The `narration-data` and `narration-handoff` volumes stay, and with them the record, so the next `up -d` carries on where it stopped. The Mac's work folders (`~/narration-data`) stay too. Episodes already published stay in Audiobookshelf; delete one there by hand if it shouldn't be up.

`down -v` would delete the volumes, and the record with them: the next night would start from an empty record, so it would queue the articles already published and could record and publish them again. To go back to an earlier version instead, check it out and rebuild: `git checkout <commit> && docker compose -f deploy/compose.yaml up -d --build`.

## Verified

To be filled in from the checks in Plan 3, Task 9 Step 4:

- **Egress from `narration-agent`:** _(not yet run)_
- **A real session refuses built-ins:** _(not yet run)_
- **The service refuses bad requests:** _(not yet run)_
- **The narration tokens hold** (from inside `narration-runner`): _(not yet run)_

**Build and start on Docker Desktop on Brian's Mac (linux/amd64), 2026-11-02**, with `agentbox:latest` built from agentbox's `agentbox-base-switch` branch, fake secrets and `MAC_HOST=host.docker.internal`:

- Both images built from `docker compose -f deploy/compose.yaml build` (Claude Code 2.1.316, Python 3.13.15 for the runner's two projects).
- `narration-agent` started: firewall `verified: example.com blocked`, `api.anthropic.com reachable`, `api.github.com blocked`, `base hosts blocked`; then `serving /handoff for http://host.docker.internal:8770/agent/mcp, prompt /opt/narration/agent_prompt.md`, as node.
- `narration-runner` started: firewall resolved `www.thenewatlantis.com` and `ntfy.sh` and verified `example.com` and `api.github.com` blocked; then `schedule --nights 1` ran as node, created `/data/record.sqlite`, and slept (`hrtimer_nanosleep`, its wait for 01:00). `status` worked through `exec -u node`.
- `/data` and `/handoff` were node's, mode 700, in both containers. `docker compose stop` stopped each in under a second.
