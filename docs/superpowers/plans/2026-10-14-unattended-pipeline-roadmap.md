# Unattended Pipeline: Roadmap and Plan 0 (Spikes)

> **For agentic workers:** this file is the index. The plans it lists are separate files; each is executed with superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Spec:** `docs/superpowers/specs/2026-10-14-unattended-podcast-pipeline-design.md` (approved: PR #7, merged 2026-10-14). Executors read the spec as well as their plan.

**Why several plans:** the spec covers four repos and two machines. Each plan below produces working, tested software on its own, and can be reviewed and merged by itself.

| Plan | What it delivers | Repo(s) | Who runs it | Depends on |
|---|---|---|---|---|
| **0. Spikes** (this file) | Answers to the spec's open questions, before code is built on guesses | none (throwaway) | S1 and S4: an agent here. S2 and S3: Brian, on the Mac, one evening | — |
| **1. Prerequisites** (`2026-10-14-plan-1-prerequisites.md`) | Skill rename; New Atlantis metadata fixes; agentbox entrypoint hook, GitHub switch, and uv | kokoro-reader, article-assistant, agentbox | agents (one PR per repo) | — |
| **2. Narration server** (`2026-10-14-plan-2-narration-server.md`) | The `narration` MCP server: work items, the six agent tools, runner tools, the quality gate, bearer-token auth | kokoro-reader | agents; Brian installs it on the Mac at the end | S1 |
| **3. Runner, agent container, deploy** (`2026-11-02-plan-3-runner-and-deploy.md`) | Discovery and sources, the record (SQLite), the nightly flow, publishing and pushes, the `narration-agent` session service, both container images, compose and launchd | kokoro-reader (+ agentbox `AGENTBOX_BASE`, compose on the home server) | agents; Brian deploys | Plans 1 and 2, and S2–S4. Written 2026-11-02, after the spikes |

**Changes the plans make to the spec:** two, recorded here for Brian's veto.

1. The spec has `narration-agent` holding "the agent token", a single standing token. Plan 2 instead makes the token **per work item**:
   - the runner's `open` call mints it;
   - it stops working at `finish` or `finalize`.

   A hijacked agent session can then reach only its own article, and no agent token sits in the container between runs. *If that's wrong:* nothing user-visible changes. The cost is one value the runner passes to the session service with each work id.

2. The spec's `finish` contract writes a substitution as `{"from": "XIV", "to": "the Fourteenth", "why": "roman numeral"}`. Plan 2's `finish` takes **`{"original": "XIV", "spoken": "the Fourteenth", "why": "roman numeral"}`**: `from` is a Python keyword, so it can't be a plain pydantic field or a tool argument, and the plan's names say which side is which. The same keys are in `result.json` and `state.json`. Plan 3 and the skill are written from the plan's keys, not the spec's. *If that's wrong:* nothing user-visible changes. The cost is an alias on one pydantic field (`from` can't be a Python name), and the rename wherever the keys are read.

**Rough cost (an estimate, not measured):** Plans 1 and 2 are 14 tasks for agents. At the level of recent builds, that's roughly 2–4M tokens including per-task reviews. Plan 0 costs a few cents of Claude usage plus an evening of the Mac.

---

## Plan 0: Spikes

Each spike answers one open question from the spec. Anything built here is **throwaway**; only the answer is kept. Record each answer as a comment on the tracking issue (`kokoro-reader` issue "Unattended pipeline: spike results", opened by the first spike agent) and in this file's "Results" section.

### S1: Can a `claude -p` session be limited to one MCP server's tools, with every built-in tool off?

**Run by:** an agent in the assistant's container, where `claude` 2.1.299 and `uv` are available. It costs a few cents of Brian's subscription.

**Flags to test** (from `claude --help`, 2026-10-14):
- `--tools ""` disables all built-in tools;
- `--strict-mcp-config` uses only the servers given in `--mcp-config`;
- `--permission-mode dontAsk` refuses anything not pre-allowed;
- `--allowedTools` names the tools that are pre-allowed;
- `--output-format json` returns usage and cost;
- `--bare` skips hooks, plugins and CLAUDE.md discovery.

- [ ] **Step 1: Write the probe server**, `/tmp/<scratch>/s1/probe_server.py`. It uses MCP Python SDK **2.x**, in which FastMCP is renamed `MCPServer` (`mcp.server.mcpserver`); this was checked against 2.3.0. It also checks that a tool can read the request's `Authorization` header through `ctx.headers`.

```python
# probe_server.py — throwaway. Run: uv run --no-project --with "mcp==2.3.0" --with uvicorn python probe_server.py
import uvicorn
from mcp.server.mcpserver import MCPServer, Context

server = MCPServer("probe")

@server.tool()
def echo(text: str, ctx: Context) -> dict:
    """Return the text, plus whether an Authorization header arrived."""
    headers = ctx.headers or {}
    return {"text": text, "auth": headers.get("authorization", "")}

if __name__ == "__main__":
    uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=8799)
```

- [ ] **Step 2: Write the MCP config,** `s1/mcp.json`, with a bearer header:

```json
{"mcpServers": {"probe": {"type": "http", "url": "http://127.0.0.1:8799/mcp",
  "headers": {"Authorization": "Bearer probe-token-123"}}}}
```

- [ ] **Step 3: Run a session that is asked to break out:**

```bash
claude -p "First run the shell command 'id'. Then read the file /etc/hostname. Then fetch https://example.com. Finally call the echo tool with the text 'hi' and report exactly what it returned." \
  --bare --tools "" --strict-mcp-config --mcp-config s1/mcp.json \
  --allowedTools "mcp__probe__echo" --permission-mode dontAsk \
  --output-format json > s1/result.json
```

- [ ] **Step 4: Check the result.** The pass criteria are:
  - the session reports that it had no way to run a shell command, read a file or fetch a URL;
  - the `echo` call succeeded, with `auth` equal to `Bearer probe-token-123`;
  - `result.json` carries `total_cost_usd` and `usage`.

  Also run it once **without** `--tools ""` to confirm that the flag is what removes the built-ins. Record the exact working command line.
- [ ] **Step 5: Test skill loading under `--bare`.** Put a one-line skill in a temp project's `.claude/skills/probe-skill/SKILL.md` and see whether a `--bare` session can use it. If it can't, record that Plan 3 must pass the skill text with `--append-system-prompt` (verify that flag exists: `claude --help | grep append-system-prompt`).
- [ ] **Step 6: Record the results and delete the scratch directory.**

### S2 + S3: Does the Mac wake on schedule with the lid closed, and does MLX work from a LaunchAgent?

**Run by:** Brian, on the MacBook, one evening. Both are answered by a single overnight test. The commands are fish-safe, and each `bash -c` holds the bash-only parts.

- [ ] **Step 1: Schedule a wake for 00:55 nightly** (as root, reversible):

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00
pmset -g sched        # shows the repeating wake
```

- [ ] **Step 2: Install a test LaunchAgent that runs at 01:00.** It holds the Mac awake for 10 minutes, records one sentence with Kokoro, and logs the result:

```bash
mkdir -p ~/Library/Logs/narration-spike
cat > ~/Library/LaunchAgents/net.robergb.narration-spike.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>net.robergb.narration-spike</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-lc</string>
    <string>export PATH=$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH; date; command -v uv || echo 'uv not on PATH'; pmset -g batt | head -2; caffeinate -i -t 600 &amp; cd ~/tools/kokoro &amp;&amp; echo "The spike test worked." &gt; /tmp/spike.txt &amp;&amp; uv run python text_to_speech.py /tmp/spike.txt --output /tmp/spike.wav &amp;&amp; ls -l /tmp/spike.wav; date</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>1</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/robergb/Library/Logs/narration-spike/out.log</string>
  <key>StandardErrorPath</key><string>/Users/robergb/Library/Logs/narration-spike/err.log</string>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/net.robergb.narration-spike.plist
launchctl kickstart gui/$(id -u)/net.robergb.narration-spike    # run once now, as a daytime check
```

- [ ] **Step 3: That evening, leave the Mac plugged in with the lid closed,** the way it would normally sit overnight.
- [ ] **Step 4: Next morning, paste these to the assistant:**

```bash
cat ~/Library/Logs/narration-spike/out.log ~/Library/Logs/narration-spike/err.log
pmset -g log | grep -E " (Wake|DarkWake|Sleep) " | grep -E "00:5|01:0|01:1" | tail -20
```

  **Pass criteria:**
  - a 01:00 entry shows the WAV written, which proves MLX works under launchd;
  - the power log shows the Mac awake from about 00:55 through the job, which proves the scheduled wake works with the lid closed.

  If the WAV was written during a `DarkWake`, note how long the Mac stayed awake.
- [ ] **Step 5: Clean up:**

```bash
launchctl bootout gui/$(id -u)/net.robergb.narration-spike
rm ~/Library/LaunchAgents/net.robergb.narration-spike.plist
sudo pmset repeat cancel        # only if Plan 3 will set its own schedule
```

### S4: How does The New Atlantis's `/issues` page list an edition's articles?

**Run by:** an agent in the assistant's container. Read-only web fetches.

- [ ] **Step 1: Fetch the page.** `curl -sS -A "Mozilla/5.0 …Chrome/130…" https://www.thenewatlantis.com/issues -o issues.html` returned 2.3 MB on 2026-10-14. Find the block for "No. 86 Fall 2026", and the element that groups each issue's article links.
- [ ] **Step 2: Write down the selector or pattern that yields No. 86's article URLs,** and check the list against the feed's nine 2026-09-08 items. Record whether a separate per-issue page exists, which would be cheaper to fetch nightly than 2.3 MB.
- [ ] **Step 3: Check one web-only piece.** For the 2026-10-07 feed item "Are We Really on the Path to AGI?", record whether its page carries an edition ("No. 86 – Fall 2026" or similar) or none. That tells Plan 3 how often the "web-only from the start date" rule applies.
- [ ] **Step 4: Save a trimmed fixture** for Plan 3's tests: the No. 86 block, plus one older issue block, saved as `tests/fixtures/new_atlantis_issues_sample.html`. Commit it on a branch only when Plan 3 starts.

## Results

*(The spike agents and Brian fill this in.)*

- **S1 (2026-10-14, an agent here, 13 `claude -p` calls, about $0.27 at list price on the subscription):**
  - ⚠️ **Don't use `--bare`.** It reads only `ANTHROPIC_API_KEY` or `apiKeyHelper`, and never OAuth or `CLAUDE_CODE_OAUTH_TOKEN`, so on the subscription it stops with "Not logged in". Without `--bare`, `CLAUDE_CODE_OAUTH_TOKEN` is used: a fake one produced `401 OAuth access token is invalid`, which proves it was read. Step 3's command above was written before this was known.
  - **The working locked-down flag set:**

    ```
    claude -p "<prompt>" --tools "" --strict-mcp-config --mcp-config <cfg.json> --allowedTools "<mcp tools>" --permission-mode dontAsk --setting-sources "" --settings '{"disableAllHooks":true}' --no-session-persistence --max-budget-usd <cap> --output-format json
    ```

    The `init` event listed only the MCP tools, with the server `connected`, and none of the user's other MCP servers or plugins. The session reported that it couldn't run a shell command, read a file or fetch a URL.
  - ⚠️ **`--tools ""` is the real control. `dontAsk` is not:** without `--tools ""`, `Bash id` ran even though `--allowedTools` named only the MCP tools.
  - **The tool sees the bearer header** from `--mcp-config` through `ctx.headers`.
  - **Tool errors:** in SDK 2.3.0 a plain exception reaches the session only as "Error executing tool …". `mcp.server.mcpserver.exceptions.ToolError` passes its message through. Plan 2 now converts its refusals.
  - **Usage:** `--output-format json` carries `total_cost_usd`, which is a list-price equivalent on the subscription, plus `usage`, `modelUsage.<model>.costUSD`, `num_turns`, `terminal_reason`, `api_error_status` and `permission_denials`.
  - **Skills:** pass the skill's text with `--append-system-prompt "$(cat SKILL.md)"`, which works with every built-in off. With `--setting-sources ""`, project skills aren't discovered.
  - **The MCP 2.x client is** `mcp.client.Client(streamable_http_client(url, http_client=create_mcp_http_client(headers=…)))`. That's now in Plan 2, Task 8. The SDK's HTTP client is `httpx2`.
  - **The Host allow-list matches exactly.** A Host off the list gets 421 and a bad Origin gets 403. The Content-Type check runs first (400), so a test must send `Content-Type: application/json`. Claude Code's HTTP MCP client sends no Origin.
- **S2 + S3:**
- **S4 (2026-10-14, an agent here, 12 fetches):**
  - **`/issues` lists every issue on one server-rendered page.** That's 86 `div[data-issue-id]` blocks inside `div.issue-catalog`, in **ascending** order, so the latest issue has the highest number, not the first position. The same data is in an inline `const TNA_ISSUES = [...]` JSON: per issue `number`, `name`, `url`, and `sections[].articles[] {url, title, subtitle, gated}`. It matched the HTML for all 86 issues.
  - **Fetching it is cheap with compression:** about 194 KB gzipped (2.3 MB raw), and Cloudflare caches it.
  - **There's no cheaper per-issue source.** `/issues/<slug>` serves the same full catalog. The REST `issue` objects list no articles, and no taxonomy links articles to issues.
  - **No. 86 (Fall 2026) has 9 articles,** and they are exactly the feed's nine 2026-09-08 items.
  - **The feed's 2026-10-07 piece, "Are We Really on the Path to AGI?", is web-only.** It's in no issue, and its header `aside` shows a date instead of a season. It has no "web-only" label anywhere: the only signal is the missing `/issues/` link in that `aside`.
  - **The article's edition is the header `aside` link,** `/issues/no-86-fall-2026`. A page-wide search is wrong: the site-wide nav links the current issue ("No. 86 – Fall 2026") on every page, and "Related" cards link other issues. Plan 1 Task 2 was revised for this.
  - **Dates:** trust JSON-LD `datePublished`, which equals the feed's pubDate. The visible "Published online on …" text can be days later.
  - **"Gated" (subscriber-only) articles still serve their full text.** `zoning-alone` has 4,415 words in its body. 8 of No. 86's 9 articles are gated.
  - **The fixture for Plan 3** (blocks No. 85 and No. 86, plus a trimmed `TNA_ISSUES`, 39.7 KB) is kept in the assistant workspace at `local/kokoro-pipeline/new_atlantis_issues_sample.html` (the parser scripts are beside it, in `s4-scripts/`), to be copied into `tests/fixtures/` when Plan 3 starts.
- **S2 + S3 (2026-11-02, Brian's MacBook, compressed to an evening: on power, external display unplugged, lid closed):**
  - **S3 passes.** A LaunchAgent started with `launchctl kickstart` ran `text_to_speech.py` from `~/tools/kokoro-reader` and wrote the WAV in 7 s, so MLX gets the GPU under launchd.
  - **S2 is only half a pass.** A `sudo pmset schedule wake` woke the closed Mac on time (17:50:00, an RTC wake), but it went back to sleep at 17:50:45 ("Clamshell Sleep", once powerd's wake linger timed out). The job set for five minutes later was missed and ran only when the lid was opened.
  - **Decision (Brian):** drop the closed-lid track for now; he often leaves the lid open overnight. With the lid open the Mac still idle-sleeps, so Plan 3 keeps it awake on power (its Decision 1).
