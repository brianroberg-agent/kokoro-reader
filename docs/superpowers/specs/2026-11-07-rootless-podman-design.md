# Running services rootless on the home server: the narration pipeline first

**Status:** approved by Brian 2026-11-07 18:35, relayed by `assistant`, with two changes, both folded in below:
1. The egress chain goes in with R3 and is tested before the real secrets arrive.
2. The account gets no IPv6 at all.

Written by the `kokoro-reader` session.
**Decided by Brian:** option A (17:53): rootless Podman, a service account per service, and the operating agent working as that account over ssh. Dry run 3 is held until it can run on this setup.

## Why

Every container on the home server runs on the root snap Docker daemon. Anyone in the `docker` group is root in all but name. Until now the pipeline's operating agent has been a Claude Code session running as root on the server.

Brian's goal, from his note "Rootless containers for the agent stack" (2026-11-05): **"a container that gets broken out of lands in an account that owns nothing else, and each account's network reach can be limited by one rule."** His 17:46 question added the second half: the coding agent that sets a service up should run with less privilege too.

## Goals

1. **A recipe for running one service under its own unprivileged account,** written so the next service repeats it. The account has no `sudo`, `docker` or `lxd`, runs rootless Podman, and its containers start at boot.
2. **The narration pipeline moves onto it first**: `narration-agent` and `narration-runner`, with agentbox's egress firewall inside each container as now.
3. **One rule can limit the account's network reach on the host,** whatever its containers do.
4. **The operating agent is given the account and nothing more.** That's this Mac session, running commands over ssh with a key only that account accepts. It isn't kept from root by any barrier: Brian's own Mac user may still reach the server (see "The old routes to root", route 3).
5. **Dry run 3, then the acceptance run, happen on the new setup.**

## Non-goals

- Designing for a particular second service. Brian names it later.
- Moving any other container off root Docker.
- Changing how the pipeline works: the compose file, images and firewall profiles stay. The only change is who runs them and how they start.
- Moving the pipeline to agentbox's agents VM. That was the option Brian didn't choose.

## Anti-goals: these would make it a failure even if it ran

- The account, or the agent's key, gives a way back to root on the host.
- A container gets more network reach than it has under root Docker today.
- A secret is printed, pasted, sent in a message or committed while things are moved.
- The root deployment is removed before the new one has run a dry run.

## What's already proven (Brian's home-tech-docs, the rootless Podman proof of concept on the home server, 2026-11-06)

- **Podman:** version 4.9.3 is installed. Rootless containers run with resource limits, `NoNewPrivileges`, dropped capabilities and a read-only root.
- **Docker Compose:** the real Docker Compose (v5.5.1), pointed at the account's Podman socket, ran a project end to end, including builds, a named network and `depends_on: service_healthy`. The snap's `docker-compose` can't reach that socket, and `podman-compose` 1.0.6 ignores `service_healthy`.
- **Known limits:**
  - Restart policies don't survive a reboot by themselves.
  - Files a container writes to a bind mount as a non-root user end up owned by sub-ids on the host.
  - A published port sees every client as the container's own address.
  - Short image names fail for pulls.
- **Network reach:** a rootless container reaches the internet, the LAN and the server's LAN address. It doesn't reach the server's `127.0.0.1`.

## Not yet proven: the first on-host tests, in order

Each test has its fallback named. None of them needs root, so they all run as the account once Brian's first step is done.

1. **Rootless egress carries the account's uid.**
   - **Why it matters:** with slirp4netns (Podman 4.9's rootless default), a container's outbound connections are opened on the host by a helper process. That process should run as the account, which is what makes the one-rule limit work.
   - **Check:** while a container fetches something slow, `ss -tnp` as the account shows the host-side socket owned by `slirp4netns` under the account's uid.
   - **Fallback:** match the account's sub-id range as well. The design already does (see "The one-rule limit").
2. **The host has what agentbox's firewall needs.**
   - **The firewall uses** `iptables`, `ipset` and `-m set`, which need the `ip_set`, `ip_set_hash_net` and `xt_set` kernel modules. A rootless container can't load modules.
   - **Check:** `lsmod` as the account.
   - **Fallback:** root step R2 loads them at boot.
   - **Also checked here:** `catatonit` (compose's `init: true`) is installed, and there's disk space for the images under the account's home.
3. **agentbox's firewall works under rootless Podman.**
   - **Run:** `up -d narration-agent`. The test uses placeholder secrets files made from the repo's `*.env.example` files, so Brian's secrets aren't needed yet.
   - **Expected:** the startup lines from docs/pipeline.md, Deploy, step 7: `verified: base hosts blocked`, `profile=restricted applied`, `dropping privileges to node`, then `serving /handoff for ...`.
   - **What's different under Podman:** DNS inside a compose network is Podman's aardvark-dns, not Docker's `127.0.0.11`, so the firewall takes the path that reads `resolv.conf`.
4. **Outbound reach from a rootless container.**
   - **The Mac's narration port over the tailnet:** `run --rm narration-runner curl -s http://<MAC_HOST>:8770/health` returns `{"ok":true}`.
   - **Audiobookshelf on the LAN:** the same kind of one-off `curl` from the runner.
   - Both come back through the firewall's grants, as under root Docker.
5. **The base image resolves.**
   - **The question:** the pipeline's Dockerfiles say `FROM agentbox:latest`. Rootless images belong to the account, so it builds agentbox itself, and the build needs that name to resolve to the account's locally built `localhost/agentbox:latest`.
   - **Fallback:** tag it explicitly, or pass the base image as a build argument.
6. **Restarts work under Podman.**
   - **Why it matters:** the runner runs one night and exits, and `restart: unless-stopped` starts the next night. Under Podman that restart is Podman's job, and the proof of concept didn't test it for containers that compose started.
   - **Check:** a throwaway container with the same policy that exits after a few seconds is started again.
   - **Fallback:** a user systemd timer owns the runner's nightly start.

## The design

### The account

- **Name:** `narration` (my call; the recipe names each account after its service).
- **Groups:** none beyond its own.
- **Login:** an ordinary shell, reached only by key.
- **Sub-ids:** 65536 subordinate uids and gids. Ubuntu's `useradd` allocates them; root step R1 checks.
- **Linger on,** so its systemd user manager, and with it the containers, runs without a login and starts at boot.

What lives in its home:
- `~/kokoro-reader`: a clone of the public repo.
- `~/agentbox`: agentbox's source. The repo is private, so this session copies it from the Mac's clone with `git archive` over ssh, and the account holds no GitHub credential.
- `~/.local/bin/docker-compose`: the standalone Compose binary.
- `~/secrets`: mode 700, with `runner.env` and `agent.env` at mode 600.
- `~/.config/systemd/user/`: the boot unit.

Images and volumes live in the account's Podman storage.

### Compose, not Quadlet (my call)

- **What runs it:** the same `deploy/compose.yaml`, run by Docker's standalone `docker-compose` binary against the account's Podman socket (`systemctl --user enable --now podman.socket`, `DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock` in the account's shell profile).
- **Version:** pinned to a release at least 7 days old (Brian's homelab rule), installed with its published checksum.
- **Why:**
  - The proof of concept ran this route end to end.
  - The compose file, the container names and every command in docs/pipeline.md stay the same.
  - Rollback to root Docker is the same file.

### Starting at boot

A user unit, `narration.service` (`Type=oneshot`, `RemainAfterExit=yes`, after `podman.socket`), runs `docker-compose -f ~/kokoro-reader/deploy/compose.yaml up -d`. Linger starts it at boot.

Until the acceptance run, `up -d` must start `narration-agent` only. The runner joins once Brian has listened to the episode. How the unit tells them apart is left to the plan; a compose profile set from `deploy/.env` is the likely way.

### Secrets

- **Where they go:** they move from `/root/narration` to `~narration/secrets`. Brian copies them as root with `install -o narration -m 600`, so they're never shown, typed again or sent anywhere.
- **Who can read them:** the account can read them, because compose reads the env files, just as root could before. The rule doesn't change: the agent never prints, pastes or sends a secret, and doesn't open those files.
- **`no-new-privileges`:** the setting goes back on (`NARRATION_NO_NEW_PRIVILEGES` empty). The snap's refusal of it doesn't apply under Podman.

### The operating agent's access

- **The key:** a new ed25519 key on the Mac, `~/.ssh/id_ed25519-narration`, made for this and used for nothing else. It has no passphrase, because the agent uses it unattended; that's the same as the existing `agentbox-mac` key.
- **The Mac's ssh config:** a `Host narration` entry for the server and user `narration`, with `IdentitiesOnly yes` and `ForwardAgent no`, following the entries already there.
- **What the account accepts:** its `authorized_keys` takes only that key, with `from="<the Mac's tailnet address>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc`.
- **No command restriction,** because the agent needs a shell to build and operate.
- **What the agent can do on the server:** only what `narration` can. No root, no Docker socket, no other account's files.
- **Unknown until tried:** whether this session's sandbox and auto mode allow `ssh` and reading the key file. If they don't, Brian adds a permission rule; the agent doesn't work around a block.

### The old routes to root (builder check, 18:15)

The new route gives the agent only `narration`. Goal 4 holds fully only when the old routes are closed too. There are three:

1. **The root Claude Code session on the server.** It has already stopped (it left the session list on 2026-11-07), and since 17:39 no worker directs another worker by message. What may remain is whatever root holds: a Claude login, and the credential root used to clone the private agentbox repo.
2. **The root deployment and its secrets:** `narration-agent` under snap Docker, its volumes, and `/root/narration`. Brian runs its rollback and removes it by hand. No agent does either.
3. **This Mac session's own reach.** It runs as Brian's own Mac user, the same as his own sessions; the bot launcher changes only the git and GitHub identity. So it can use any key that user has.
   - If one of those keys, or Tailscale SSH, lets that user into the server, the agent could reach the server as Brian's account there, which is in `docker` and so root in all but name.
   - The new key's `authorized_keys` options don't limit that route.
   - The agent's rule is to use only the `narration` route. That's a rule, not a barrier.

**What's closed, and when** (answered 18:17 by `assistant` on Brian's behalf; he can change it when he approves):
- **Routes 1 and 2 close in root step R5,** once the rootless setup has run for real.
  - **Why:** by then the root deployment, `/root/narration` and root's credentials for this work are only a rollback, and Brian's goal ("lands in an account that owns nothing else") needs them gone.
  - **Basis:** his 17:53 choice of option A and his bike-queue goal.
- **Route 3 stays open,** and the design says so.
  - **Why:** Brian's tier 2 (2026-11-06) already accepts that an agent on his Mac runs with his user's reach, so closing it would be hardening beyond this work.
  - **What this means for the claim:** the agent is *given* the `narration` route; nothing stops it reaching root by another route.

### The one-rule limit (my policy, for veto)

**How it's entered.** On the host, a chain `svc-narration` is entered from `OUTPUT` by the owner match: `-m owner --uid-owner narration -j svc-narration`. A second jump with the account's sub-id range (`--uid-owner <first>-<last>`) catches a process that breaks out of a container as a sub-id. The chain is the one place the account's reach is set. For the next service, it's one more chain and its jumps.

**What the chain does, in order:**
1. Lets replies and related traffic through (`ESTABLISHED,RELATED`). Without this the rule would cut the agent's own ssh session, whose server side runs as `narration`.
2. Lets name lookups to the local resolver through (`-o lo`, port 53), and rejects the rest of `lo`.
3. Lets through the pipeline's LAN and tailnet grants: the Mac's narration port and Audiobookshelf's `address:port`. These are the same grants as in `deploy/.env`.
4. Rejects the rest of the private ranges: `10/8`, `172.16/12`, `192.168/16`, `100.64/10` (the tailnet) and `169.254/16`. That covers the server's own LAN and tailnet addresses.
   - Added in the build, after the automated security review: multicast (`224/4`), the reserved block with broadcast (`240/4`), and `0/8`. Multicast reaches devices at home, and `0.0.0.0` reaches the host itself.
5. Lets the internet through over IPv4. Builds need registries, PyPI, npm and GitHub, and each container's own allowlist stays agentbox's job.

**No IPv6 for the account (Brian, 18:35).**
- **The rule:** an `ip6tables` chain entered by the same two owner matches rejects everything.
- **Why not reject only private IPv6 ranges:** those wouldn't cover a home network's public IPv6 addresses, which would count as internet and stay reachable. The server has carried IPv6 traffic before.
- **Effect:** the internet works over IPv4, and the agent's ssh uses IPv4 (`AddressFamily inet` in the Mac's ssh config entry).

**What's blocked.** A breakout lands in an account that can reach the internet and two ports on the home network, and nothing else at home.

**What it doesn't stop: the internet is open (accepted by Brian, 2026-11-07, for this service only).**
- agentbox's firewall limits each container. A process that breaks out of a container skips that firewall and lands in `narration`, where only this chain applies, and the chain lets the whole internet through.
- The account holds the pipeline's secrets: the Claude login, the Audiobookshelf key, the ntfy token and the narration runner token. A breakout could read them and send them to any address on the internet.
- **Why this is accepted here:** Brian, 2026-11-07: this "is OK for the present application, which doesn't handle sensitive data. It may not be acceptable for future applications."
- **The tighter policy, for a service that needs it:**
  - Open the internet to the account only while it builds images.
  - The rest of the time, allow only the hosts the service uses (for the pipeline: the Anthropic API, the source site and the ntfy host), the way agentbox's allowlist does inside the container.
  - The cost: the chain then needs a build-time switch, and a host's addresses resolved and kept current outside any container.
- **For each next service,** the recipe asks this question before its chain is written: does an open internet expose anything that matters?

**Keeping the rules across reboots.** How the server keeps its firewall rules is checked at the first ssh: ufw, netfilter-persistent, or none (alongside Docker's own rules). The persistence goes in root step R3 to match.

**Root never runs anything the account can write.** The chain's rules and their persistence are commands Brian runs from the plan, and files root writes under `/etc`. They're never a script in the account's home or its clone of the repo, which the account (or a breakout into it) could change before root runs it.

### Cutover and rollback

- **Side by side until cutover:** the root deployment (`narration-agent` under snap Docker) keeps running while everything above is built and tested with placeholder secrets.
- **At cutover** (root step R3):
  - Brian copies the secrets and stops the root `narration-agent`. That's `stop`, never `down -v`, so its volumes stay.
  - The rootless agent starts with the real secrets.
- **The record starts fresh** under the account (my call, for veto). Dry runs never moved an article's status, and discovery fills the record again on the first run.
- **Rollback:**
  - As root, `docker compose -f deploy/compose.yaml up -d narration-agent` in `/root/kokoro-reader` brings the old one back, until Brian removes it.
  - Under the account, `systemctl --user disable --now narration.service`.
  - The egress chain comes off by deleting its jump rules.

## Brian's root steps, in order, each as late as possible

Each step comes with its exact commands and the output to check, in the plan.

- **R1, first; nothing on the server can start without it.**
  - Create the account with no extra groups. Check its sub-id ranges in `/etc/subuid` and `/etc/subgid`.
  - `loginctl enable-linger narration`.
  - Install `~narration/.ssh/authorized_keys` with the public key this session gives him.
  - Check `sshd -T` for `AllowUsers`/`AllowGroups`, and add the account if either is set.
  - Check whether Tailscale SSH is on for the server (`tailscale status --json`, the `SSH` setting, or the tailnet policy). If it is, the policy, not `authorized_keys`, decides who gets in as whom.
- **R2, only if test 2 finds them missing:** load `ip_set`, `ip_set_hash_net` and `xt_set` at boot (`/etc/modules-load.d/`), and `modprobe` them now. If `catatonit` is missing, also `apt install catatonit`.
- **R3, at cutover,** after tests 1–6 pass, in two halves (Brian, 18:35: from R3 the account runs a real Claude session with the real login, so the chain must already hold):
  - **R3a, the chain first.**
    - Brian installs the IPv4 and IPv6 chains, their jump rules and their persistence.
    - This session then checks, still with placeholder secrets:
      - a LAN address other than the grants is refused;
      - a port on the server's own `127.0.0.1` other than DNS is refused;
      - an IPv6 address is refused;
      - the Mac's narration port and Audiobookshelf still answer;
      - the agent's ssh session survives.
  - **R3b, then the secrets,** once those checks pass:
    - Copy the two secrets files into `~narration/secrets`.
    - Stop the root `narration-agent`.
- **R5, after the real run is published and he has listened:** close old routes 1 and 2.
  - Remove the root deployment: `down` without `-v` first, then the volumes when he's sure.
  - Remove `/root/narration` and any Claude login and GitHub credential root holds for this work.
  - Route 3 (his Mac user's own reach) stays open.

## What gets built, and where

**Before R1, on the Mac and in the repo (after this design is approved):**
- **kokoro-reader, on a branch:**
  - docs/pipeline.md, Deploy, rewritten for the account and rootless Podman, with the snap Docker section kept as the rollback reference until cutover.
  - `deploy/env.example` updated (the secrets folder, `no-new-privileges`).
  - The user unit, plus whatever the boot-time choice of services needs.
  - Tests for what the repo can test, red then green.
  - Delivered as one PR with an Opus single-pass review and labels. The PR body says what breaks, how Brian would notice and the rollback.
  - The repo is public, so it uses generic names ("the home server", "the Mac's tailnet address"). Real hostnames and addresses stay in STATUS.md, `~/.ssh/config` and `deploy/.env`.
- **On the Mac:** the key pair and the `~/.ssh/config` entry.

**After R1, as the account:** tests 1–6, the agentbox and pipeline builds, and the boot unit. Then, in order:
1. R3a, then the chain checks.
2. R3b.
3. Dry run 3 on the new setup, with Brian's OK.
4. The real run, and Brian listens.
5. The runner joins the boot unit.
6. R5.

**After it works: the recipe.** The per-service account recipe goes on a page in home-tech-docs: the root steps as a template, the egress chain, and what the tests found. It's written from what actually ran, and delivered as its own PR there.

The recipe says plainly that the pipeline's chain leaves the internet open, and that this was accepted because the pipeline handles no sensitive data. It then makes the choice explicit for the next service, which may need the tighter policy above ("What it doesn't stop").

## Left to the plan

- The exact unit files.
- How `up -d` tells the agent from the runner before acceptance.
- The Compose release and checksum.
- The test commands and their expected output.
- The wording of the docs.
