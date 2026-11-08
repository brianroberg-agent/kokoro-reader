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
| Hostnames (every port) | `RUNNER_ALLOW`: the source site, and the ntfy host when nothing else runs at its address | none |
| Host:port grants | the Mac's narration port, plus `RUNNER_LAN` (Audiobookshelf, or a host that shares its address with other services, by address and port) | the Mac's narration port |
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
| `NTFY_TOPIC_URL`, `NTFY_TOKEN` | `runner.env` | An ntfy topic for the pipeline, under any name, with its own access token; the runner only posts, so a token that may only write to the topic is enough. `NTFY_TOPIC_URL` is the topic's full URL, `https://<ntfy host>/<topic>`, and the runner posts to it with the token as a bearer token. |
| `CLAUDE_CODE_OAUTH_TOKEN` | `agent.env` | `claude setup-token`, run on the Mac (it works with the Max subscription). The token lasts about a year, per agentbox's notes. |

**The Claude login expires.** Record the day the token was made here, and set a reminder for 11 months later to make a new one:

- Claude token created: _(date)_. Reminder set for: _(date)_.

If it lapses anyway, or the token in `agent.env` is wrong, the first session of the night can't authenticate. The runner then pushes "Claude login failed", with "Claude Code could not authenticate (CLAUDE_CODE_OAUTH_TOKEN missing, expired or invalid)", and stops the night. The article goes back to `failed` with that as its `last_error` and without a failed night counted against it, so it comes first the next night, and the push comes again each night until the token is fixed.

To renew it, run `claude setup-token` again, put the new value in `agent.env` in the secrets folder, and recreate the agent so it reads the file again (a plain `restart` keeps the old environment): `docker compose -f deploy/compose.yaml up -d --force-recreate narration-agent`. Then update the date above.

**If Claude Code can't reach the API** for another reason (it reports the request as overloaded, rate-limited or failed) before the session's first narration tool call, the runner pushes "Claude API unreachable" with the session's error and stops the night the same way: the article goes back to `failed` without a failed night, and comes first the next night. If that push comes every night, recreate the agent (see "Name lookups" above). Once the session has called a tool, the article's text is in it, so a failed API request from then on counts against the article like any other failure (an article that made every request fail would otherwise stop every night).

## Deploy

The pipeline runs in its own unprivileged account on the home server, under rootless Podman ("Deploy, rootless Podman", next). The earlier setup, root's Docker, follows it ("Deploy, root Docker"). It stays as the way back until root step R5 removes it. The design is `docs/superpowers/specs/2026-11-07-rootless-podman-design.md`.

### Deploy, rootless Podman

**What changes and what doesn't:**
- **The same:** the compose file, the images and agentbox's firewall inside each container.
- **The account:** `narration` runs them. It has no `sudo`, `docker` or `lxd`.
- **How they run:** rootless Podman runs the containers, and Docker's standalone Compose drives them through the account's Podman socket. A systemd user unit starts them at boot.
- **The host's limit:** one chain limits what the account may reach (`svc-egress`, "The account's reach" below).
- **Operating it:** the agent that operates the pipeline does so over ssh as `narration`, with a key only that account accepts.

Brian does the root steps (R1 to R5). **Root never runs anything from the account's home or its clone of this repo**, which the account could change first. `svc-egress` and its units are fetched from a pinned commit and checked against their sha256 sums.

#### The root steps

**R1, first. The account, and the agent's way in:**

```bash
useradd --create-home --shell /bin/bash --user-group narration
id narration                               # only the narration group
grep narration /etc/subuid /etc/subgid     # one line each, 65536 ids
loginctl enable-linger narration           # its containers run without a login, and start at boot
install -d -m 700 -o narration -g narration ~narration/.ssh
echo 'from="<the Mac'"'"'s tailnet address>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc <the agent'"'"'s public key>' \
  | install -m 600 -o narration -g narration /dev/stdin ~narration/.ssh/authorized_keys
sshd -T | grep -Ei '^(allow|deny)(users|groups)'   # if anything is listed, let the account in
```

Also check whether Tailscale SSH is on for the server. If it is, the tailnet's policy, not `authorized_keys`, decides who gets in as whom.

**R2, only if the account's checks find them missing.** agentbox's firewall needs the `ip_set`, `ip_set_hash_net` and `xt_set` kernel modules, which a rootless container can't load. Compose's `init: true` needs `catatonit`.

```bash
printf '%s\n' ip_set ip_set_hash_net xt_set > /etc/modules-load.d/svc-egress.conf
modprobe -a ip_set ip_set_hash_net xt_set
apt install catatonit
```

**R3a, the account's chain**, before any real secret reaches the account (see "The account's reach" below). The commit and the five sums come from the PR or the operator.

```bash
sha=<commit>
for f in svc-egress svc-egress@.service svc-egress-check@.service svc-egress-check@.timer user-egress.conf; do
  curl -fsSLO "https://raw.githubusercontent.com/brianroberg/kokoro-reader/$sha/deploy/rootless/$f"
done
sha256sum svc-egress svc-egress@.service svc-egress-check@.service svc-egress-check@.timer user-egress.conf
                                                             # compare with the published sums
install -m 755 svc-egress /usr/local/sbin/svc-egress
install -m 644 svc-egress@.service svc-egress-check@.service svc-egress-check@.timer /etc/systemd/system/
install -D -m 644 user-egress.conf "/etc/systemd/system/user@$(id -u narration).service.d/svc-egress.conf"
install -d -m 755 /etc/svc-egress
printf '%s\n' 'GRANTS="<MAC_HOST>:<MAC_PORT> <Audiobookshelf address>:<port>"' \
  'RESOLVERS="<the host'"'"'s upstream resolvers, from resolvectl dns>"' > /etc/svc-egress/narration.conf
systemctl daemon-reload
systemctl enable --now svc-egress@narration.service svc-egress-check@narration.timer
iptables -S svc-narration; ip6tables -S svc-narration; iptables -S OUTPUT | head -n 4
```

The grants are the same as in `deploy/.env`: the Mac's narration port, and Audiobookshelf's address and port. `RESOLVERS` names the upstream DNS servers the host uses (`resolvectl dns`, IPv4 only): the containers' lookups go to them through the account, and port 53 is open to these and no others.

The timer runs `svc-egress check` every 30 seconds. If a reload of the host's own firewall has taken the chain or its jumps away, the check puts them back, and says so in the journal (`journalctl -u svc-egress-check@narration`).

**To change a grant later**, edit `/etc/svc-egress/narration.conf`, then `systemctl reload svc-egress@narration.service`. That re-applies the chain in place. Don't `restart` it: the account's user manager requires that unit, so a restart restarts the manager too, and every container under it. A runner mid-article would leave its article `in_progress`.

If ufw or another tool manages the host's firewall, reload it. Then, within 30 seconds, run the three `-S` commands again: the chain must be there, either kept or put back by the check.

**R3b, the secrets, once the chain checks pass.** Copy the two files into the account with `install`, so they aren't shown or typed again. Then stop the root deployment, with `stop`, never `down -v`:

```bash
install -d -m 700 -o narration -g narration ~narration/secrets
install -m 600 -o narration -g narration <root's secrets folder>/runner.env <root's secrets folder>/agent.env ~narration/secrets/
docker compose -f deploy/compose.yaml --profile schedule stop   # in root's checkout: both, the runner too if it ran
```

**R5, once the rootless setup has published for real and Brian has listened.** Remove the root deployment: `down` first (without `-v`), then its volumes once he's sure. Also remove root's secrets folder, and any Claude login or GitHub credential root holds for this work.

#### The account's side

As `narration`, from its home:

1. **Podman's socket, and the variable that points Compose at it:**

   ```bash
   systemctl --user enable --now podman.socket
   ```

   Then put `export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock` near the top of `~/.bashrc`, above the line that returns early for non-interactive shells, so commands run over ssh get it too.

2. **Docker Compose**, the standalone binary, from Docker's GitHub releases. Pick a release at least 7 days old, check it against its `.sha256`, and put it at `~/.local/bin/docker-compose`.
   - Ubuntu's snap `docker-compose` can't reach the account's socket.
   - `podman-compose` ignores `depends_on: condition: service_healthy`.
   - Under this account, every `docker compose ...` command in this guide is `docker-compose ...`.

3. **Short image names.** agentbox's Dockerfile starts `FROM node:...`, and the host defines no registries to search. Write `~/.config/containers/registries.conf` with one line:

   ```toml
   unqualified-search-registries = ["docker.io"]
   ```

4. **The sources:**
   - **This repo:** clone it to `~/kokoro-reader`.
   - **agentbox:** the repo is private, and the account holds no GitHub credential. The operator copies its `image/` folder in from a checkout elsewhere (`git archive origin/main image | ssh narration 'mkdir -p ~/agentbox && tar -x -C ~/agentbox'`). Then:

     ```bash
     podman build -t agentbox:latest ~/agentbox/image/
     ```

5. **`deploy/.env`**, from `deploy/env.example`, as in "Deploy, root Docker", step 4, with three differences:
   - `NARRATION_SECRETS_DIR=/home/narration/secrets`;
   - `NARRATION_NO_NEW_PRIVILEGES` empty (Podman keeps `no-new-privileges` on);
   - `COMPOSE_PROFILES` empty until the acceptance run.

6. **Build, and start at boot:**

   ```bash
   cd ~/kokoro-reader
   docker-compose -f deploy/compose.yaml --profile schedule build
   mkdir -p ~/.config/systemd/user
   ln -s ~/kokoro-reader/deploy/rootless/narration.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now narration.service
   ```

   The unit runs a plain `up -d`. With `COMPOSE_PROFILES` empty, that starts `narration-agent` alone. Check its start as in "Deploy, root Docker", step 7.

7. **The acceptance run**, as in "Deploy, root Docker", step 8. Then turn on the schedule: set `COMPOSE_PROFILES=schedule` in `deploy/.env`, and `systemctl --user restart narration.service`. The runner's log shows `next run at <time>`.

#### The account's reach

`svc-egress` (`deploy/rootless/`) writes one chain, `svc-narration`, on the host:
- **How it's entered:** from `OUTPUT`, by an owner match on the account's uid, and a second on its sub-id range.
- **Why that covers the containers:** a rootless container's outbound connections are opened on the host by a helper process running as the account, so the match covers everything its containers send. The sub-id jump covers a process that breaks out of a container as a sub-id.

**IPv4, in order:**
1. Replies go through.
2. Name lookups go through to the host's own stub resolver (for the account's shell) and to the conf's `RESOLVERS`, which a compose network's DNS server forwards to. Port 53 is open to these and no others.
3. The grants in `/etc/svc-egress/narration.conf` go through, before loopback: a grant to one of the server's own addresses goes out over loopback.
4. The rest of the host's own loopback is rejected.
5. The LAN, the tailnet, link-local, multicast, broadcast and `0.0.0.0/8` are rejected. Multicast reaches devices at home, and `0.0.0.0` reaches the host itself.
6. The internet goes through.

**IPv6:** everything is rejected.

**What the tests cover:** `pipeline/tests/test_svc_egress.py` checks:
- the chain's order;
- the jumps at the top of `OUTPUT`;
- that a second `apply` leaves one pair of jumps;
- that it stops before it changes anything on a bad grant or resolver, a missing conf or account, or a missing sub-id line;
- that it also stops when the account has more than one sub-id range, since one jump covers one range;
- that a failed load leaves that family as it was, and a re-apply keeps the jumps in place;
- that `check` puts back a flushed chain and jumps moved down, and leaves an intact chain alone;
- that `remove` works after the account is gone.

These run against a fake `iptables`. What the real host does is checked there and recorded under "Verified".

**The account's user manager depends on the chain.** `user-egress.conf` makes the manager require `svc-egress@narration.service` and start after it. The tests check what the drop-in says; the host check is under "Verified". What that covers:
- **At boot,** the manager, and with it the boot unit's containers, starts after the chain is in.
- **Stopping the egress unit** stops the manager and its containers.

What it doesn't cover:
- **A firewall reload** can take the chain away while everything keeps running. `svc-egress check` puts it back within 30 seconds.
- **A container started some other way while the manager isn't running** has no chain behind it. An example is `podman run` in an ssh session after the chain's unit failed. Check `systemctl status svc-egress@narration` before working around a stopped manager.

**What it doesn't stop: the internet is open to the account.**
- agentbox's firewall limits each container. A process that breaks out of one skips that firewall, and lands in an account whose chain lets the whole internet through.
- The account holds the pipeline's secrets, so such a process could send them anywhere.
- Brian accepted this for the pipeline, which "doesn't handle sensitive data. It may not be acceptable for future applications." (2026-11-07)
- **The home's own public address counts as internet too.** Through the router's hairpin NAT, the account can reach any port forwarded to a device at home, and the router's admin page if it answers on that side. Rejecting that address would take a rule kept current with the address; it isn't done here.
- A service that needs more would open the internet only while it builds, and otherwise allow only the hosts it uses.

### Deploy, root Docker

The earlier setup, kept as the way back until R5. On the home server, from scratch:

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

   These use the default folder, `/srv/narration`. To keep the files elsewhere, use that folder in the commands and set `NARRATION_SECRETS_DIR` to it, as an absolute path, in `deploy/.env` (step 4). A relative one would put the secrets inside the repo, beside `compose.yaml`. Docker's snap package needs this ("Docker from the snap package", below).

   compose stops with "env file <the folder>/runner.env not found" if a file is missing.

4. **Write `deploy/.env`** from `deploy/env.example` (`cp deploy/env.example deploy/.env`). It is git-ignored. Fill in:
   - `MAC_HOST`: the Mac's tailnet address, `tailscale ip -4` on the Mac. Use the address, not the name: the firewall makes its grant when the container starts, and a tailnet name may not resolve inside a container. The narration server accepts the address in the Host header, since the plist's `NARRATION_ALLOWED_HOSTS` lists `TAILNET_IP:8770`.
   - `MAC_PORT`: leave at `8770` unless the plist's `NARRATION_PORT` differs.
   - `RUNNER_ALLOW`: `www.thenewatlantis.com,<the ntfy host>`, plus Audiobookshelf's hostname if `AUDIOBOOKSHELF_URL` uses one. A hostname here is required: the runner doesn't start if it can't be resolved.

     The firewall opens **every port** at the addresses a hostname here resolves to. So leave out a hostname that resolves to the Docker host itself, or to any machine running other services (ntfy and Audiobookshelf behind a reverse proxy on the home server, say): that would open all of them to the runner. Put its `address:443` in `RUNNER_LAN` instead, and keep the hostname in `AUDIOBOOKSHELF_URL` and `NTFY_TOPIC_URL`: only the firewall's grant changes. For example, with both behind a proxy at 192.168.1.20: `RUNNER_ALLOW=www.thenewatlantis.com` and `RUNNER_LAN=192.168.1.20:443`.
   - `RUNNER_LAN`: `address:port` grants, comma-separated: Audiobookshelf's if `AUDIOBOOKSHELF_URL` uses an address, and the `address:443` of any host left out of `RUNNER_ALLOW` above; otherwise empty.
   - `NARRATION_SECRETS_DIR`: empty for `/srv/narration`, or the absolute path of the folder the secrets went into in step 3.
   - `NARRATION_NO_NEW_PRIVILEGES`: empty, which leaves `no-new-privileges` on, or `false` on a Docker that refuses it (below).

   compose stops with "set MAC_HOST in deploy/.env" (or `RUNNER_ALLOW`) when one of them is unset.

5. **Check that the Mac answers** from the home server: `curl -s http://<MAC_HOST>:8770/health` gives `{"ok":true}`.

6. **Build both images, and start only the agent.** The runner's schedule waits until the acceptance run below (spec, "Acceptance": Brian listens to the first article before the schedule is turned on):

   ```bash
   docker compose -f deploy/compose.yaml --profile schedule build
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
   1. A dry run of one article: `run --dry-run --only <article URL>`. Its discovery also queues every article in scope, and its report line reads `queued  publish  <title>`, with the episode notes below it.
   2. The same article for real: `run --only <article URL>`. It reports `published` (or `published_with_notes`).
   3. Brian listens to the episode in Audiobookshelf.

9. **Start the schedule**, once the episode is good:

   ```bash
   docker compose -f deploy/compose.yaml up -d narration-runner
   docker compose -f deploy/compose.yaml logs narration-runner
   ```

   The log shows the firewall lines as the agent's did, `verified: base hosts blocked` among them, then `next run at <time>` (01:00 New York time, as an ISO time), and the runner waits. That night it takes the articles the acceptance run's discovery queued, five a night, oldest first.

`docker compose config` prints the merged configuration with the secrets from both files in it: don't paste its output anywhere.

#### Docker from the snap package

Ubuntu's snap of Docker confines the docker command, and that changes the deploy:

- **It can't read `/srv`.** compose then stops with "env file /srv/narration/runner.env not found" although the file is there. It can read the home folder of the account that runs `docker compose`, so keep the secrets in a folder there instead (mode 700, the files 600), and set `NARRATION_SECRETS_DIR` to it in `deploy/.env`: `/root/narration` when that account is root.
- **It refuses `no-new-privileges`.** Every container that sets it fails to start with `exec ...: operation not permitted`: AppArmor refuses runc's switch to the container's profile once the setting is on, and turning the container's AppArmor profile off doesn't help. Set `NARRATION_NO_NEW_PRIVILEGES=false` in `deploy/.env`. What the setting stops is a process, running as `node`, getting root or a capability back by running a setuid program or one with a file capability. Neither image has either: the last step of each Dockerfile removes the setuid and setgid bits and stops the build if one is left or a file has a capability. agentbox itself drops root with `gosu`, which isn't setuid.
- **It has its own `/tmp`.** A file written to `/tmp` outside it isn't there for a docker or compose command; pipe a compose file on standard input instead (`docker compose -f - ...`).

## Operate

Under the rootless account, every `docker compose` below is `docker-compose`, the standalone binary. Start and stop the containers there with `systemctl --user start` or `stop narration.service`; its own log is `journalctl --user -u narration.service`.

Every `exec` below has `-u node`. `exec` doesn't go through agentbox's entrypoint, so without it the command runs as root, and files it leaves in `/data` or `/handoff` would belong to root, where the node processes may then fail to write. `run --rm` does go through the entrypoint (firewall, then node), so it needs no `-u`.

**Status:** counts by status and the ten rows updated last.

```bash
docker compose -f deploy/compose.yaml exec -u node narration-runner uv run --frozen python -m narration_pipeline status
```

**A dry run of one article.** It fetches, records and checks the article and finalizes the work item, then reports what it would have done (`publish`, `hold` or `retry`, with the notes, and the reasons when it would not publish), but publishes nothing, sends no push and leaves the row `queued`. The run discovers first, and the URL must be one discovery found (now or on an earlier night); otherwise the run stops with `not discovered: <URL>`.

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

**Updating** after a `git pull`: first the Mac (pull its checkout, `uv sync --inexact`, and restart the LaunchAgent, `docs/narration-server.md`), then `docker compose -f deploy/compose.yaml --profile schedule build` and `docker compose -f deploy/compose.yaml up -d`. `--profile schedule` builds the runner too: it's in that profile, and a plain `build` (or `up -d --build`) skips it until `COMPOSE_PROFILES=schedule` is set, leaving its image older than the agent's. The plain `up -d` then starts what `COMPOSE_PROFILES` turns on. The Mac goes first because the agent's prompt, built into the image, can name what only a newer narration server accepts (the `omission` issue kind, for one); in the other order such a `finish` is refused until the Mac is updated. Do it in the daytime: a runner restarted mid-night leaves its article `in_progress`, and the next night recovers it.

## Rollback

**From the rootless account back to root's Docker, until R5:**
- As `narration`: `systemctl --user disable --now narration.service`. That stops the account's containers and keeps their volumes.
- As root, in root's checkout: `docker compose -f deploy/compose.yaml up -d narration-agent`. The runner comes back the same way, once it had been started there.
- The account's chain comes off with `systemctl disable --now svc-egress@narration.service`. This also stops the account's user manager, and any container still running under it.

**Within either setup:**

```bash
docker compose -f deploy/compose.yaml down
```

This stops and removes both containers. The `narration-data` and `narration-handoff` volumes stay, and with them the record, so the next `up -d` carries on where it stopped. The Mac's work folders (`~/narration-data`) stay too. Episodes already published stay in Audiobookshelf; delete one there by hand if it shouldn't be up.

`down -v` would delete the volumes, and the record with them: the next night would start from an empty record, so it would queue the articles already published and could record and publish them again. To go back to an earlier version instead, check it out and rebuild: `git checkout <commit> && docker compose -f deploy/compose.yaml --profile schedule build && docker compose -f deploy/compose.yaml up -d`.

## Verified

**Still open: rootless Podman on the home server.** Each of these gets checked on the host before cutover, and its result goes here:
- the account's privileges;
- whether a rootless container's egress carries the account's uid;
- agentbox's firewall under rootless Podman;
- reach to the Mac and to Audiobookshelf;
- a compose-started container restarting under Podman (the runner's nightly restart);
- the boot unit;
- the chain, with a refused LAN address, a refused loopback port and refused IPv6, while the grants and lookups still work;
- the account's user manager stopping with the egress unit, and coming back after it;
- a firewall reload, and `svc-egress check` putting the chain back.

**Still open: the home server.** Nothing here has run there yet: the deploy above end to end (the tailnet from both containers, Brian's Audiobookshelf), and the acceptance run with Brian's listen. Brian's ntfy and a login from `claude setup-token` were used on the Mac (below). Also not yet run: the spec's containment test of a real session asked to run a shell command, read a file and fetch a URL (each session's tool list is checked when it starts, and that check passed against the real init event in the sessions below).

**The whole pipeline on Brian's Mac, 2026-11-03**: the narration server with its real engines (Kokoro, the transcript check, Gemini), both containers on Docker Desktop with Brian's `claude setup-token` login and ntfy topic, the live site, and Audiobookshelf 2.37.2 in a local container:

- **The acceptance run:** a dry run, then the real run, of "The Pro-Human AI Declaration": published with notes; each session $0.04 to $0.07.
- **A push:** with the agent's container stopped, the runner sent "Narration session service not running", which reached Brian's phone, and started no article.
- **The Mac gone mid-article:** the narration server killed 25 seconds into a session left the article `in_progress`, with nothing published; the next full run's recovery marked it `failed` (one failed night), and the next run published it once.
- **A podcast that doesn't exist yet:** the runner created "The New Atlantis" with its cover before the first upload; with the episodes tagged album "The New Atlantis" and no artist, it kept its title and had no author after two episodes. (Tagged with the article's authors, it had taken the first episode's as its author.)
- **A long article:** "Zoning Alone", 29:14 of audio, checked in 9 sections; 3 record rounds, 13 minutes, $0.15.
- **The opening line:** each recording opened with the title and its byline, or the title alone for the unsigned Declaration.
- **A fix tried before `minor`:** in "Is There Life on Mars, Pennsylvania?", `finish` refused `minor` with a mispronunciation after round 1; the session added an override for the author's surname, recorded and checked round 2, and published with notes ($0.08). The Declaration's session used two rounds without a refusal, declaring the list numbers and the web address it fixed ($0.06). Neither held.

**The snap-package settings, on Docker Desktop on Brian's Mac (linux/amd64), 2026-11-03:**

- **No setuid program or file capability:** after both images were built, `find / -xdev -type f \( -perm -4000 -o -perm -2000 \)` and `getcap -r /` printed nothing in either; Debian's `su` and `passwd` were mode 755. The entrypoint still applied the firewall (`verified: base hosts blocked`, `example.com` blocked) and ran the command as `node`.
- **The build stops on a capability:** an image built on `narration-agent` that added a `cap_net_raw` program stopped at the last step, naming `/usr/local/bin/capped cap_net_raw=ep`.
- **`NARRATION_NO_NEW_PRIVILEGES`:** unset, the runner's process showed `NoNewPrivs: 1`; with `false`, `0`, and it still started as `node` and reached the test narration server through the firewall; with `no`, Docker refused the container (`invalid --security-opt 2`).
- **`NARRATION_SECRETS_DIR`:** set, compose loaded each service's env file from that folder; empty or unset, it looked in `/srv/narration`.

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
