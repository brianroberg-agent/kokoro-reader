# Rootless Podman for the narration pipeline: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** move the narration pipeline from root snap Docker on the home server into an unprivileged `narration` account running rootless Podman. Its network reach is limited on the host by one chain, and this Mac session operates it over ssh as that account.

**Architecture:**
- **What stays:** the compose file, the images and agentbox's in-container firewall.
- **How it runs:** Docker's standalone Compose binary against the account's Podman socket, started at boot by a user systemd unit, with linger on.
- **The host chain:** a small root-installed script (`svc-egress`) and two systemd units write and keep the account's egress chain. They fail closed: the account's user manager can't start without the chain.
- **Root's part:** Brian does the root steps (R1, R2 if needed, R3a, R3b, R5). Root never runs a file the account can write.

**Tech stack:**
- Podman 4.9 (rootless, slirp4netns).
- Docker Compose v2 standalone.
- systemd user and system units.
- iptables and ip6tables, owner match.
- bash, and pytest for the static and stubbed tests.

**Spec:** `docs/superpowers/specs/2026-11-07-rootless-podman-design.md` (approved 2026-11-07 with two changes: the chain goes in with R3a, before the secrets; no IPv6 for the account).

## Global Constraints

- **The repo is public.** No server hostname, IP address, home domain, Mac username or anyone's real name goes in tracked text, commit messages or the PR. Use "the home server", "the Mac's tailnet address" and `<placeholders>`. Grep the staged text before every commit (STATUS.md, "Rules for agents").
- **Root never runs a file the account can write.** Brian installs `svc-egress` and its units from a pinned GitHub commit with a published sha256, never from `~narration/`.
- **Secrets:**
  - Never print, `cat`, paste, send or commit a secret, and never open the files in `~narration/secrets/`.
  - Never paste `docker compose config` output; run it with `--quiet` only.
- **Containers and volumes:**
  - No `down -v`.
  - `narration-runner` doesn't start as a service until Brian has listened to the acceptance episode. `run --rm narration-runner ...` is fine.
- **The commit gate:** the tests and the commit go in one command, with no pipe, `;` or `||` between them, and the results are read in a separate call:
  `uv run --frozen pytest -q tests > $S/root.txt 2>&1 && (cd pipeline && uv run --frozen pytest -q > $S/pipe.txt 2>&1) && git commit ...`
- **Tests:**
  - Red/green TDD. Keep the red and green output for the PR.
  - An absolute claim in the docs needs a test, or an on-host check recorded under docs/pipeline.md, "Verified".
- **Talking to Brian:**
  - Plans, questions and results go to the `assistant` session as reports.
  - Permission prompts happen in this window.
  - No messages to any other worker session.
  - Don't work around an auto-mode block; ask Brian.
- **Third-party releases** (the Compose binary) must be at least 7 days old (Brian's homelab rule).
- **No IPv6 for the account.** The agent's ssh uses IPv4 (`AddressFamily inet`).
- **The new worktree** needs the spaCy model: `uv pip install --python .venv/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl`.

## Review Focus

1. **A reboot of the home server.**
   - **Expected:** the chain is in before the account's user manager starts, then the containers come up.
   - **Risk:** nothing in the repo can test the ordering.
   - **Pinned by:** Task 3's unit tests (`Requires=`/`After=` in the drop-in) and Task 11's check that stopping the egress unit stops the account's user manager.
2. **A firewall reload on the host (ufw, or Docker restarting) dropping the chain.**
   - **Expected:** the chain survives, or the account stops.
   - **Pinned by:** Task 8 records which firewall tool the host uses. Task 11 reloads it and checks the chain is still there.
3. **The nightly runner restart under Podman.**
   - **Expected:** the runner's exit is followed by a restart, as under Docker.
   - **Pinned by:** Task 10, step 4.
4. **The Mac reaching the server over IPv6** (a tailnet name resolving to its IPv6 address).
   - **Expected:** ssh still connects, over IPv4.
   - **Pinned by:** Task 6, step 3.
5. **The account's disk filling with images.**
   - **Expected:** the build stops with a clear error, not a half-written state.
   - **Pinned by:** Task 8 records free space. Task 9 runs `podman system df` after the builds.

---

## Part A: the repo (branch `rootless-podman`, worktree `.worktrees/rootless`)

### Task 1: The runner only starts at boot once Brian enables it

**Files:**
- Modify: `deploy/compose.yaml` (`narration-runner`: `profiles: ["schedule"]`)
- Modify: `deploy/env.example` (add `COMPOSE_PROFILES=`, with a comment: empty until the acceptance episode is good, then `schedule`)
- Test: `pipeline/tests/test_deploy_files.py`

**Interfaces:**
- Produces: a plain `up -d` starts `narration-agent` only, until `COMPOSE_PROFILES=schedule` is set in `deploy/.env`. Task 2's unit relies on this.

- [ ] **Step 1: write the failing tests**

```python
def test_runner_starts_with_up_only_once_the_schedule_profile_is_on(compose):
    assert compose["services"]["narration-runner"]["profiles"] == ["schedule"]
    assert "profiles" not in compose["services"]["narration-agent"]
    assert example_names("env.example")["COMPOSE_PROFILES"] == ""
```

Update `test_compose_settings_come_from_deploy_env`: the names in `env.example` are the compose file's `${...}` names plus `COMPOSE_PROFILES`, which compose reads from `deploy/.env` itself.

- [ ] **Step 2:** `cd pipeline && uv run --frozen pytest -q tests/test_deploy_files.py -k "profile or deploy_env"` fails with `KeyError: 'profiles'` (or the set mismatch).
- [ ] **Step 3:** add the profile, and the `env.example` entry and its comment.
- [ ] **Step 4:** the same command passes.
- [ ] **Step 5:** commit through the gate: `deploy: the runner joins up -d only with the schedule profile`.

### Task 2: The account's boot unit

**Files:**
- Create: `deploy/rootless/narration.service`
- Test: `pipeline/tests/test_deploy_files.py`

**Interfaces:**
- Consumes: Task 1's profile.
- Produces: the user unit the account links into `~/.config/systemd/user/`. Task 10 uses it.

- [ ] **Step 1: write the failing test.** Parse the unit with `configparser.ConfigParser(interpolation=None, strict=False)`, with `optionxform = str`. The default interpolation chokes on systemd's `%h` and `%t`. Check:

```python
def test_the_boot_unit_brings_up_what_deploy_env_enables():
    u = unit("rootless/narration.service")
    assert u["Unit"]["Requires"] == "podman.socket" and "podman.socket" in u["Unit"]["After"]
    assert u["Service"]["Type"] == "oneshot" and u["Service"]["RemainAfterExit"] == "yes"
    assert u["Service"]["Environment"] == "DOCKER_HOST=unix://%t/podman/podman.sock"
    assert u["Service"]["WorkingDirectory"] == "%h/kokoro-reader"
    assert u["Service"]["ExecStart"] == "%h/.local/bin/docker-compose -f deploy/compose.yaml up -d"
    assert u["Service"]["ExecStop"] == "%h/.local/bin/docker-compose -f deploy/compose.yaml stop"  # never down
    assert u["Install"]["WantedBy"] == "default.target"
```

- [ ] **Step 2:** run it. It fails because the file doesn't exist.
- [ ] **Step 3:** write the unit. Give it a `Description` that points to docs/pipeline.md.
- [ ] **Step 4:** run it. It passes.
- [ ] **Step 5:** commit: `deploy: the account's boot unit`.

### Task 3: `svc-egress`, the account's one chain, and its units

**Files:**
- Create: `deploy/rootless/svc-egress` (bash, mode 755)
- Create: `deploy/rootless/svc-egress@.service` (system unit)
- Create: `deploy/rootless/user-egress.conf` (drop-in for `user@<uid>.service.d/`)
- Test: `pipeline/tests/test_svc_egress.py`, with a stateful fake `iptables`/`ip6tables` (a small Python script on `PATH` that keeps chains and rules in a JSON file and logs each call), and a fake `id` that knows one account

**Interfaces:**
- **Produces:** `svc-egress apply <account>` and `svc-egress remove <account>`.
- **What it reads:**
  - `${SVC_EGRESS_CONF_DIR:-/etc/svc-egress}/<account>.conf`, holding `GRANTS="<ipv4>:<port> ..."`;
  - the uid, from `id -u <account>`;
  - the sub-id range, from `${SVC_EGRESS_SUBUID:-/etc/subuid}`, line `<account>:<start>:<count>`, giving `<start>-<start+count-1>`.
- **Chain name:** `svc-<account>`.
- **Exit codes:** 0 on success; 1 and nothing changed on a bad grant, a missing conf, an unknown account or no sub-id line, with stderr naming the problem.

**The rules `apply` writes**, in this exact order (spec, "The one-rule limit", and Brian's two changes):

```
iptables  -N svc-<a>            (or -F when it exists)
iptables  -A svc-<a> -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables  -A svc-<a> -o lo -p udp --dport 53 -j RETURN
iptables  -A svc-<a> -o lo -p tcp --dport 53 -j RETURN
iptables  -A svc-<a> -o lo -j REJECT
iptables  -A svc-<a> -d <ip>/32 -p tcp --dport <port> -j RETURN      (one per grant, in conf order)
iptables  -A svc-<a> -d <net> -j REJECT    for 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16
                                            (and, after the security review: 0.0.0.0/8 224.0.0.0/4 240.0.0.0/4)
iptables  -A svc-<a> -j RETURN
iptables  -I OUTPUT 1 -m owner --uid-owner <uid> -j svc-<a>
iptables  -I OUTPUT 2 -m owner --uid-owner <first>-<last> -j svc-<a>
ip6tables -N svc-<a>            (or -F)
ip6tables -A svc-<a> -j REJECT
ip6tables -I OUTPUT 1 -m owner --uid-owner <uid> -j svc-<a>
ip6tables -I OUTPUT 2 -m owner --uid-owner <first>-<last> -j svc-<a>
```

Before inserting, `apply` deletes any existing jumps to `svc-<a>` from `OUTPUT`, so running it twice leaves one pair. It checks every input before its first `iptables` call. `remove` deletes the jumps, then flushes and deletes both chains.

- [ ] **Step 1: write the failing tests** (names and what each asserts):
  - `test_apply_writes_the_v4_chain_in_order`: the fake's v4 chain equals the list above for `GRANTS="100.64.0.5:8770 192.168.50.20:13378"`, uid 1001, sub-ids `231072:65536` (so the range is `231072-296607`).
  - `test_the_account_gets_no_ipv6`: the v6 chain is `[REJECT]` alone, and both v6 jumps are there.
  - `test_jumps_sit_at_the_top_of_output`: with an existing `OUTPUT` rule in the fake, the two jumps come first, uid before range.
  - `test_apply_twice_leaves_one_pair_of_jumps`.
  - `test_a_bad_grant_stops_with_its_name_and_changes_nothing`, for `GRANTS="host.example:80"`, `"10.0.0.1"` and `"10.0.0.1:99999"`: exit 1, stderr names the grant, and the fake logged no call.
  - `test_no_subuid_line_stops_and_changes_nothing`.
  - `test_remove_leaves_no_chain_or_jump`.
  - `test_the_egress_unit_runs_apply_and_remove`: `svc-egress@.service` has `ExecStart=/usr/local/sbin/svc-egress apply %i`, `ExecStop=... remove %i`, `Type=oneshot`, `RemainAfterExit=yes` and `WantedBy=multi-user.target`. The ordering before the account's containers comes from the drop-in, not from this unit.
  - `test_the_users_manager_needs_the_chain`: `user-egress.conf` has `Requires=svc-egress@narration.service` and `After=svc-egress@narration.service`. That's fail-closed: without the chain, the account's manager and its containers don't start.
- [ ] **Step 2:** `cd pipeline && uv run --frozen pytest -q tests/test_svc_egress.py` fails because the files don't exist.
- [ ] **Step 3:** write the script (`set -euo pipefail`; validate everything first; IPv4 grants only, a dotted quad and a port from 1 to 65535), then the two unit files.
- [ ] **Step 4:** run it. It passes. Also run `shellcheck deploy/rootless/svc-egress`, if it's installed, and expect it to be clean.
- [ ] **Step 5:** commit: `deploy: svc-egress, one chain per service account, no IPv6`.

### Task 4: Docs, and the spec and plan in the repo

**Files:**
- Modify: `docs/pipeline.md`. Deploy becomes "Deploy, rootless Podman", which covers:
  - the account and its five root steps;
  - the Compose binary, `podman.socket` and `DOCKER_HOST`;
  - the boot unit and `COMPOSE_PROFILES`;
  - `svc-egress` and how to install it from a pinned commit;
  - secrets in `~/secrets`, with `NARRATION_SECRETS_DIR` set to it and `NARRATION_NO_NEW_PRIVILEGES` empty.

  "Docker from the snap package" stays, retitled as the rollback path until R5. "Rollback" covers both directions, and "Operate" gets `systemctl --user` equivalents.
- Create: `docs/superpowers/specs/2026-11-07-rootless-podman-design.md`, the approved design with generic names.
- Create: `docs/superpowers/plans/2026-11-07-rootless-podman.md`, this plan.

- [ ] **Step 1:** write the docs. Every absolute claim either points to a test from Tasks 1–3 or goes under "Verified" once Part B has checked it on the host. Until then, write "checked on the host in Task N".
- [ ] **Step 2:** grep the diff for infra names and addresses (STATUS.md's list), and expect no hits.
- [ ] **Step 3:** commit through the gate: `docs: deploy the pipeline under a rootless service account`.

### Task 5: Review and PR

- [ ] **Step 1:** `/code-review high` on the branch (Opus single pass; kokoro-reader is a personal project). Then one commit per finding, a second round only if a fix changes behaviour, and a cap of two rounds.
- [ ] **Step 2:**
  - Push without `-u` to the fork.
  - Open the PR with `--reviewer brianroberg --assignee brianroberg`, labels over REST and the review record as its first comment.
  - The body covers what breaks, how Brian would notice and the rollback, plus the red and green output. It says it isn't live until Part B, and that the on-host results will be added as they come.
- [ ] **Step 3:** report the PR to `assistant`. Don't merge it: Part B runs from the PR branch, and Brian merges when cutover has worked.

## Part B: on the host (each Brian step goes to him through `assistant`, with the exact commands filled in)

### Task 6: The agent's key and route (on the Mac)

- [ ] **Step 1:** `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519-narration -C "kokoro-reader agent"`. If the sandbox or auto mode blocks it, stop and ask Brian in this window.
- [ ] **Step 2:** append to `~/.ssh/config` a `Host narration` entry with:
  - `HostName` the server;
  - `User narration`;
  - `IdentityFile ~/.ssh/id_ed25519-narration`;
  - `IdentitiesOnly yes`, `ForwardAgent no`, `AddressFamily inet`;
  - `StrictHostKeyChecking accept-new`.
- [ ] **Step 3:** confirm `ssh -G narration` shows `addressfamily inet` and the new key.
- [ ] **Step 4:** send `assistant` a `blocked` report holding root step **R1**, with the commands filled in (the real Mac tailnet address and the public key line):
  - `useradd --create-home --shell /bin/bash --user-group narration`;
  - `id narration`, which shows only the `narration` group;
  - `grep narration /etc/subuid /etc/subgid`, which should give one line each with 65536 ids;
  - `loginctl enable-linger narration`;
  - installing `~narration/.ssh/authorized_keys` (mode 600) with `from="<mac>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc <pubkey>`;
  - `sshd -T | grep -Ei '^(allow|deny)(users|groups)'`, adding the account if anything is listed;
  - whether Tailscale SSH is on for the server;
  - copying root's `deploy/.env` to `~narration/deploy.env.root` (mode 600, owner `narration`). It holds no secrets; Task 10 reads the settings from it.

### Task 7: First contact

- [ ] **Step 1:** `ssh narration 'id; groups; loginctl show-user narration -p Linger'`. Expect only `narration`'s own group, and `Linger=yes`.
- [ ] **Step 2:** `ssh narration 'sudo -n true'` fails, and `test -w /var/run/docker.sock` fails. That's the account's privilege, checked.

### Task 8: Host facts and tests 1–2

- [ ] **Step 1, test 1:**
  - Start `podman run --rm docker.io/curlimages/curl:<pinned> --limit-rate 20k -so /dev/null https://<a large public file>` in the background.
  - While it runs, `ss -tnp` shows the host-side connection owned by `slirp4netns`, and `ps -o user= -p <pid>` is `narration`.
  - Record the result. If the owner is a sub-id, the range jump already covers it.
- [ ] **Step 2, test 2:**
  - `lsmod | grep -E '^(ip_set|ip_set_hash_net|xt_set) '` lists all three, or R2 is needed.
  - `dpkg -s catatonit` shows it installed, or R2 adds it.
  - `df -h ~` shows at least 10 GB free.
- [ ] **Step 3, the host's firewall tooling:** record `systemctl is-active ufw netfilter-persistent firewalld` and `iptables -V` (legacy or nf_tables).
- [ ] **Step 4:** if anything is missing, send R2 through `assistant` (`/etc/modules-load.d/svc-egress.conf` with the three modules, `modprobe` them now, `apt install catatonit`). Wait.

### Task 9: The account's toolchain and builds, and test 5

- [ ] **Step 1:** `systemctl --user enable --now podman.socket`. Put `export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock` in `~/.bashrc`, above its non-interactive `return`. Then `ssh narration 'echo $DOCKER_HOST'` prints it.
- [ ] **Step 2:**
  - Install `docker-compose`: the newest release at least 7 days old, from Docker's GitHub releases, into `~/.local/bin`.
  - Check its `.sha256` before `chmod +x`.
  - `docker-compose version` prints it.
- [ ] **Step 3, the sources:**
  - `git clone -b rootless-podman https://github.com/brianroberg-agent/kokoro-reader.git ~/kokoro-reader`.
  - agentbox: `git -C ~/tools/agentbox archive origin/main image | ssh narration 'mkdir -p ~/agentbox && tar -x -C ~/agentbox'`, plus its commit id in `~/agentbox/COMMIT`.
- [ ] **Step 4, short names:**
  - agentbox's Dockerfile starts `FROM node:${NODE_VERSION}-bookworm`, and the host's `registries.conf` has no search registries, so that pull fails.
  - Write `~/.config/containers/registries.conf` with `unqualified-search-registries = ["docker.io"]`. It's user-level, so no root is needed.
  - `podman pull node:22-bookworm-slim` then resolves to `docker.io/library/...`. Remove that image afterwards.
- [ ] **Step 5, test 5:**
  - `podman build -t agentbox:latest ~/agentbox/image/`.
  - Then `cd ~/kokoro-reader && docker-compose -f deploy/compose.yaml build`. Both images build, and the base-image check passes.
  - If `FROM agentbox:latest` resolves to `docker.io/library/agentbox:latest` (Podman's Docker-compat API can do that) and fails, try this first:
    - make the base a build argument, `ARG BASE_IMAGE=agentbox:latest` / `FROM ${BASE_IMAGE}`, in both Dockerfiles;
    - set `BASE_IMAGE=localhost/agentbox:latest` from `deploy/.env` through compose's `build.args`;
    - write it red/green in `test_deploy_files.py`, and record why.
- [ ] **Step 6:** `podman system df`, recorded.

### Task 10: Tests 3, 4 and 6 with placeholder secrets, and the boot unit

- [ ] **Step 1, the settings:**
  - Make `~/secrets-placeholder/` (mode 700), with `runner.env` and `agent.env` copied from the examples (mode 600, empty values).
  - Write `deploy/.env` from `~/deploy.env.root`:
    - same `MAC_HOST`, `MAC_PORT`, `RUNNER_ALLOW` and `RUNNER_LAN`;
    - `NARRATION_SECRETS_DIR=/home/narration/secrets-placeholder`;
    - `NARRATION_NO_NEW_PRIVILEGES` empty;
    - `COMPOSE_PROFILES` empty.
  - `docker-compose -f deploy/compose.yaml config --quiet` exits 0.
- [ ] **Step 2, test 3:** `up -d`, then `ps`, which shows only `narration-agent`. Its logs show `verified: base hosts blocked`, `profile=restricted applied`, `dropping privileges to node` and `serving /handoff for http://<MAC_HOST>:8770/agent/mcp`.
- [ ] **Step 3, test 4:**
  - `run --rm narration-runner curl -s http://<MAC_HOST>:8770/health` gives `{"ok":true}`.
  - The same with Audiobookshelf's URL gives an HTTP status, not a timeout.
  - `run --rm narration-runner curl -s -m 5 https://example.com` is refused by the container's firewall.
- [ ] **Step 4, test 6:**
  - A throwaway compose project in `~/restart-test`: one `docker.io/library/alpine:<pinned>` service, `restart: unless-stopped`, command `sleep 5`.
  - After 20 s, `podman inspect` shows `RestartCount` of at least 2.
  - Remove it with `down`.
  - If it doesn't restart, stop and send the fallback (a user timer for the runner) to `assistant` as a question.
- [ ] **Step 5, the boot unit:**
  - `ln -s ~/kokoro-reader/deploy/rootless/narration.service ~/.config/systemd/user/`.
  - `systemctl --user daemon-reload && systemctl --user enable --now narration.service`.
  - `systemctl --user stop narration.service` stops the agent; `start` brings it back.
- [ ] **Step 6:** add the results to the PR body ("On the host") and report to `assistant`.

### Task 11: R3a, the chain, then its checks

- [ ] **Step 1:** send R3a through `assistant`, with the PR branch's head commit and the sha256 of the three files at it:
  - Fetch them from `https://raw.githubusercontent.com/brianroberg-agent/kokoro-reader/<sha>/deploy/rootless/...` and check the sha256s.
  - `install -m 755 svc-egress /usr/local/sbin/`, and `install -m 644 svc-egress@.service /etc/systemd/system/`.
  - `install -D -m 644 user-egress.conf /etc/systemd/system/user@<uid>.service.d/svc-egress.conf`.
  - `/etc/svc-egress/narration.conf` (mode 644, root) with `GRANTS="<MAC_HOST>:<MAC_PORT> <Audiobookshelf ip:port>"`.
  - `systemctl daemon-reload && systemctl enable --now svc-egress@narration.service`.
  - `iptables -S svc-narration`, `ip6tables -S svc-narration`, and the first two lines of `iptables -S OUTPUT`.
  - If ufw (or another tool) manages the firewall: reload it, then repeat the `-S` checks.
  - **If a reload drops the chain:** `Requires=` guards only startup, so the containers would keep running without the chain. Decide from Task 8's finding, as part of this root step:
    - add a drop-in on that tool's service, `PartOf=` or `ExecStartPost=systemctl restart svc-egress@narration.service`, so the chain comes back after every reload;
    - or, for ufw, put the rules in `/etc/ufw/before.rules` and `before6.rules` instead.
    - Then reload again and check.
- [ ] **Step 2, the checks as `narration`, with placeholder secrets still in:**
  - The ssh session is still alive.
  - The Mac's `/health` answers, from the host and from `run --rm narration-runner`.
  - Audiobookshelf answers.
  - `curl -s -m 5 http://<a LAN address other than the grants>` fails at once, not with a timeout. The exact message depends on the reject: "Connection refused" or "Operation not permitted".
  - `curl -s -m 5 http://127.0.0.1:<a port listening on the host other than 53>` fails at once.
  - `curl -6 -s -m 5 https://example.com` fails at once.
  - `curl -4 -s -m 5 https://example.com` succeeds; the internet is open by design.
  - Name lookups work.
- [ ] **Step 3, fail closed:** ask Brian (through `assistant`) to run `systemctl stop svc-egress@narration.service`.
  - Expect: `user@<uid>.service` stops with it (`Requires=`), and so do the containers. An open ssh session stays, since it isn't part of the user manager.
  - Then he runs `systemctl start user@<uid>.service`, which starts the chain first (`Requires=` with `After=`).
  - Check: `iptables -S svc-narration` is back, and the agent comes back up.
- [ ] **Step 4:** record everything in the PR and in docs/pipeline.md, "Verified" (a commit through the gate). Report to `assistant`.

### Task 12: R3b, the secrets and cutover

- [ ] **Step 1:** send R3b through `assistant`:
  - `install -d -m 700 -o narration -g narration ~narration/secrets`;
  - `install -m 600 -o narration -g narration /root/narration/runner.env /root/narration/agent.env ~narration/secrets/`;
  - in `/root/kokoro-reader`, `docker compose -f deploy/compose.yaml stop narration-agent` (never `down -v`).
- [ ] **Step 2:**
  - Set `NARRATION_SECRETS_DIR=/home/narration/secrets` in `deploy/.env`, and `ls -l ~/secrets` shows two files, mode 600, owned by `narration`, without opening them.
  - `systemctl --user restart narration.service`.
  - The agent's log shows the four startup lines again.
- [ ] **Step 3:** remove `~/secrets-placeholder`. Report to `assistant`.

### Task 13: Dry run 3 on the new setup

- [ ] **Step 1:** ask through `assistant` for Brian's OK.
- [ ] **Step 2:** `cd ~/kokoro-reader && docker-compose -f deploy/compose.yaml run --rm narration-runner uv run --frozen python -m narration_pipeline run --dry-run --only <the article URL> > ~/acceptance-dry-run-3.log 2>&1; echo "exit $?"`. Run it in the background on the host, so the ssh session can end: `systemd-run --user --unit=dry-run-3 --setenv=DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock" --working-directory="$HOME/kokoro-reader" bash -c '...'`. The calling shell expands both variables. `systemd-run` starts from a clean environment and doesn't read `.bashrc`.
- [ ] **Step 3:** report the exit code, the report lines and notes, the work item id, the cost and turns, and whether the Gemini upload was slow, to `assistant`.

### Task 14: After dry run 3 (each step on Brian's word, through `assistant`)

- [ ] **The real run:** `run --only <URL>`. Brian listens.
- [ ] **The schedule:** `COMPOSE_PROFILES=schedule` in `deploy/.env`, then `systemctl --user restart narration.service`. The runner's log shows `next run at ...`.
- [ ] **R5:** Brian removes the root deployment (`down` without `-v`, then the volumes when he's sure), `/root/narration`, and any Claude login or GitHub credential root holds for this work.
- [ ] **The merge:** Brian merges the PR. The account's clone moves to upstream `main`: set `origin` to `https://github.com/brianroberg/kokoro-reader.git`, fetch, and check out `main`.
- [ ] **The recipe:** a home-tech-docs page and PR. It holds the account recipe, the root steps as a template, `svc-egress`, what the tests found, and the open-internet choice put to each next service.
