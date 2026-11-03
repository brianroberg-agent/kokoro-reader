# The pipeline on the home server

Each night the runner looks for new articles, has a Claude Code session record each one on the Mac's narration server, and publishes the finished episodes to Audiobookshelf. It runs as two containers on the home server, both built on agentbox's image and egress firewall:

- **`narration-runner`** runs `python -m narration_pipeline schedule --nights 1`. It holds the Audiobookshelf key, the ntfy token and the narration runner token. It keeps the record in the `narration-data` volume (`/data/record.sqlite`).
- **`narration-agent`** runs the session service, which takes one request at a time from the `narration-handoff` volume and runs a locked-down `claude -p` with only the six narration tools. It holds only the Claude login.

The Mac runs the narration server (`docs/narration-server.md`). The files are `deploy/compose.yaml`, the two Dockerfiles in `deploy/narration-runner/` and `deploy/narration-agent/`, and the three examples `deploy/env.example`, `deploy/runner.env.example` and `deploy/agent.env.example`.

Each source is a `[[source]]` entry in `pipeline/sources.toml`. Its `extractor` names the article-assistant extractor its site needs, and the runner refuses a name it doesn't know (`new-atlantis` is the one there is); article-assistant itself picks the extractor from each article's URL, so the key documents the source rather than choosing anything. A new site needs its extractor in article-assistant first, then its name added to `EXTRACTORS` in `pipeline/narration_pipeline/config.py`. Each source also names its podcast's cover (`cover`, a square image in `pipeline/covers/`, required). When the podcast isn't in Audiobookshelf yet, the runner creates it under its own name, in a folder of that name in the first podcast library, with this cover, before the first upload. After each publish, a podcast with no cover gets this one (a cover already set, by hand or earlier, is left alone). Audiobookshelf builds a podcast's title and author from its episodes' MP3 tags (an empty podcast's title from its first episode's album, its author from their artist), so each episode is tagged with its title, the podcast's name as album and no artist: the podcast keeps its name and has no author. In case an older episode's tags rename it anyway, the runner finds a podcast by its title or by its folder's name, and sets the title back after each publish (`pipeline/covers/README.md`).

Every recording opens with the article's title and who wrote it ("Where Did the Commons Go? by Ada Quill"), or the title alone when the page shows no author under it. The runner puts that line before the text it sends to the Mac, as a heading (`# ...`) with no final period: the word check then doesn't take the title's capitalised words for the article's names, and a title starting with a number keeps it. The names are article-assistant's `byline`, the names shown under the title; for a site whose extractor has no `byline`, they are its `authors`.

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

   Both images' builds stop with "agentbox:latest predates AGENTBOX_BASE (agentbox PR #14); rebuild it from agentbox main" when the base image is older than that change.

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

6. **Build both images, and start only the agent.** The runner's schedule waits until the acceptance run below (spec, "Acceptance": Brian listens to the first article before the schedule is turned on):

   ```bash
   docker compose -f deploy/compose.yaml build
   docker compose -f deploy/compose.yaml up -d narration-agent
   ```

7. **Check the agent's start:**

   ```bash
   docker compose -f deploy/compose.yaml ps
   docker compose -f deploy/compose.yaml logs narration-agent
   ```

   The log shows agentbox's firewall lines, among them `verified: base hosts blocked` (the base hosts are closed: `AGENTBOX_BASE=off` took effect), ending in `profile=restricted applied` and `dropping privileges to node`, then `serving /handoff for http://<MAC_HOST>:8770/agent/mcp, prompt /opt/narration/agent_prompt.md`. If the container keeps restarting, its log says why.

   Then check that a runner container, not only the host, reaches the Mac over the tailnet: `docker compose -f deploy/compose.yaml run --rm narration-runner curl -s http://<MAC_HOST>:8770/health` gives `{"ok":true}`. (`run --rm` starts a one-off runner container through agentbox's entrypoint, so the firewall applies and the command runs as node; its firewall lines come first in the output, and they too include `verified: base hosts blocked`. If it can't start, the output says why: for example a hostname in `RUNNER_ALLOW` that doesn't resolve, or a missing variable in `runner.env`, named but never its value.)

8. **The acceptance run:** one real article, first as a dry run, then published (the commands are in Operate, below):
   1. A dry run of one article: `run --dry-run --only <article URL>`. Its discovery also queues every article in scope, and it reports `would publish` with the episode notes.
   2. The same article for real: `run --only <article URL>`. It reports `published` (or `published_with_notes`).
   3. Brian listens to the episode in Audiobookshelf.

9. **Start the schedule**, once the episode is good:

   ```bash
   docker compose -f deploy/compose.yaml up -d narration-runner
   docker compose -f deploy/compose.yaml logs narration-runner
   ```

   The log shows the firewall lines as the agent's did, `verified: base hosts blocked` among them, then `next run at <time>` (01:00 New York time, as an ISO time), and the runner waits. That night it takes the articles the acceptance run's discovery queued, five a night, oldest first.

`docker compose config` prints the merged configuration with the secrets from both files in it: don't paste its output anywhere.

## Operate

Every `exec` below has `-u node`. `exec` doesn't go through agentbox's entrypoint, so without it the command runs as root, and files it leaves in `/data` or `/handoff` would belong to root, where the node processes may then fail to write. `run --rm` does go through the entrypoint (firewall, then node), so it needs no `-u`.

**Status:** counts by status and the ten rows updated last.

```bash
docker compose -f deploy/compose.yaml exec -u node narration-runner uv run --frozen python -m narration_pipeline status
```

**A dry run of one article.** It fetches, records and checks the article and finalizes the work item, then reports what it would have done (`would publish`, with the notes), but publishes nothing, sends no push and leaves the row `queued`. The run discovers first, and the URL must be one discovery found (now or on an earlier night); otherwise the run stops with `not discovered: <URL>`.

```bash
docker compose -f deploy/compose.yaml run --rm narration-runner \
  uv run --frozen python -m narration_pipeline run --dry-run --only <article URL>
```

A run of one article asks the Mac once: if it doesn't answer, the run stops with `the narration server isn't reachable` (a run without `--only` keeps asking every five minutes until 05:00). A real run of one article is the same command without `--dry-run`. Another run in progress makes it stop with `another run is in progress`.

**Reading the record.** The image has no `sqlite3` command; Python's module reads it, read-only:

```bash
docker compose -f deploy/compose.yaml exec -u node narration-runner python3 -c "
import sqlite3
db = sqlite3.connect('file:/data/record.sqlite?mode=ro', uri=True)
for row in db.execute('SELECT updated_at, status, failed_nights, rounds, claude_cost_usd, last_error, title '
                      'FROM articles ORDER BY updated_at DESC LIMIT 20'):
    print(*row, sep='  |  ')"
```

The columns are in `pipeline/narration_pipeline/record.py`. The ones worth reading: `status` (`baseline`, `queued`, `in_progress`, `published`, `published_with_notes`, `held`, `failed`), `failed_nights`, `last_error`, `remaining_issues` (the notes published with the episode, or the reasons for a hold), `episode_id`, and the costs: `claude_cost_usd` and `claude_tokens` are the totals of the sessions whose results the runner received, a retried article's earlier ones included, added as each result arrives (a session the runner stopped waiting for, past its timeout, isn't in them); `gemini_cost_usd` and `run_seconds` are its last attempt's.

**Logs:** `docker compose -f deploy/compose.yaml logs --since 24h narration-runner` (and `narration-agent`).

**"Narration session service not running".** The agent's session service touches `/handoff/heartbeat` every 2 seconds, also while a session runs. Before each article the runner reads it: when it is missing or more than 60 seconds old, the runner sends this push and starts no article that night; an article whose session got no result while the heartbeat stopped goes back to `failed` without a failed night. Look at `docker compose -f deploy/compose.yaml ps` and the agent's log, then start it again: `docker compose -f deploy/compose.yaml up -d narration-agent`. When the runner stops waiting for a result, it withdraws the request if the service hasn't taken it yet, and the service answers a request older than its timeout plus 300 seconds with "stale request" instead of starting a session.

**Updating** after a `git pull`: first the Mac (pull its checkout, `uv sync --inexact`, and restart the LaunchAgent, `docs/narration-server.md`), then `docker compose -f deploy/compose.yaml up -d --build`. The Mac goes first because the agent's prompt, built into the image, can name what only a newer narration server accepts (the `omission` issue kind, for one); in the other order such a `finish` is refused until the Mac is updated. Do it in the daytime: a runner restarted mid-night leaves its article `in_progress`, and the next night recovers it.

## Rollback

```bash
docker compose -f deploy/compose.yaml down
```

This stops and removes both containers. The `narration-data` and `narration-handoff` volumes stay, and with them the record, so the next `up -d` carries on where it stopped. The Mac's work folders (`~/narration-data`) stay too. Episodes already published stay in Audiobookshelf; delete one there by hand if it shouldn't be up.

`down -v` would delete the volumes, and the record with them: the next night would start from an empty record, so it would queue the articles already published and could record and publish them again. To go back to an earlier version instead, check it out and rebuild: `git checkout <commit> && docker compose -f deploy/compose.yaml up -d --build`.

## Verified

**Still open: the home server.** Nothing here has run there yet: the deploy above end to end (the tailnet from both containers, Brian's Audiobookshelf), and the acceptance run with Brian's listen. Brian's ntfy and a login from `claude setup-token` were used on the Mac (below). Also not yet run: the spec's containment test of a real session asked to run a shell command, read a file and fetch a URL (each session's tool list is checked when it starts, and that check passed against the real init event in the sessions below).

**The whole pipeline on Brian's Mac, 2026-11-03**: the narration server with its real engines (Kokoro, the transcript check, Gemini), both containers on Docker Desktop with Brian's `claude setup-token` login and ntfy topic, the live site, and Audiobookshelf 2.37.2 in a local container:

- **The acceptance run:** a dry run, then the real run, of "The Pro-Human AI Declaration": published with notes; each session $0.04 to $0.07.
- **A push:** with the agent's container stopped, the runner sent "Narration session service not running", which reached Brian's phone, and started no article.
- **The Mac gone mid-article:** the narration server killed 25 seconds into a session left the article `in_progress`, with nothing published; the next full run's recovery marked it `failed` (one failed night), and the next run published it once.
- **A podcast that doesn't exist yet:** the runner created "The New Atlantis" with its cover before the first upload, and its title held after Audiobookshelf's scan of the first episode.
- **A long article:** "Zoning Alone", 29:14 of audio, checked in 9 sections; 3 record rounds, 13 minutes, $0.15.
- **The opening line:** each recording opened with the title and its byline, or the title alone for the unsigned Declaration.

**The containers on Docker Desktop on Brian's Mac (linux/amd64), 2026-11-02**, with `agentbox:latest` built from agentbox's `agentbox-base-switch` branch (agentbox PR #14), fake secrets, `MAC_HOST=host.docker.internal`, and a test narration server on the host:

- **Egress from `narration-agent`:** `example.com`, `sentry.io`, `statsig.com`, `api.github.com` and the host's other ports were blocked; `api.anthropic.com` and the Mac's narration port were reachable.
- **A session without a Claude login:** with no `CLAUDE_CODE_OAUTH_TOKEN`, the session ended as a failed result within 5 seconds rather than hanging, and the result held no token.
- **The service refuses bad requests:** a request with the item id `../../etc` went to `rejected/`, and no session started for it.
- **The runner end to end:** the runner container, with the real `claude -p` through a session service run on the host and the local Audiobookshelf below, published an article with notes (41 turns, $0.42).
- **The narration tokens hold** (from inside `narration-runner`): the narration server's runner endpoint refused a call without a token and a call with an agent token.
- **Both containers together, sharing the handoff volume** (and again after the images were rebuilt): with no Claude token, and with a well-formed but invalid one, the session ended with "Claude Code could not authenticate (CLAUDE_CODE_OAUTH_TOKEN missing, expired or invalid)"; the runner sent one "Claude login failed" push, stopped the night, and counted no failed night against the article.
- **Build and start:** both images built from `docker compose -f deploy/compose.yaml build` (Claude Code 2.1.316; Python 3.13 for the runner's two projects). `narration-agent` started with the firewall's `verified: example.com blocked`, `api.anthropic.com reachable`, `api.github.com blocked` and `base hosts blocked`, then `serving /handoff for http://host.docker.internal:8770/agent/mcp, prompt /opt/narration/agent_prompt.md`, as node. `narration-runner` resolved `www.thenewatlantis.com` and `ntfy.sh`, ran `schedule --nights 1` as node, created `/data/record.sqlite` and waited for 01:00; `status` worked through `exec -u node`. `/data` and `/handoff` were node's, mode 700, in both, and `docker compose stop` stopped each in under a second.

**Real sessions and a real Audiobookshelf, on Brian's Mac, 2026-11-02:**

- **Real agent sessions:** `run --only` through the session service with the real `claude -p` 2.1.316 (the S1 flags, stream-json), the test narration server (fake engines) and the local Audiobookshelf published two articles clean (15 turns, $0.16; and 11 turns, $0.11), each with its episode id saved.
- **Audiobookshelf's API**, against Audiobookshelf 2.37.2 in a local container: an upload, the episode appearing, its title, description and date set and read back, and a second publish that found the episode and uploaded nothing.
