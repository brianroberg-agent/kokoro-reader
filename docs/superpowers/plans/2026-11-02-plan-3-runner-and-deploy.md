# Plan 3: Runner, Agent Container and Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each night, new New Atlantis articles become checked episodes in Brian's Audiobookshelf podcast with nothing for him to do, and he hears about an article only when it needs his judgment.

**Architecture:** A new uv project in `pipeline/` (package `narration_pipeline`), separate from the root project because the root depends on MLX and the runner runs on Linux.
- **Pure logic, tested without a network:** discovery parsing and the scope rule, the record's state machine, the night's flow (over injected fakes), message text, and the schedule.
- **Thin adapters:** HTTP for the feed and listing, a subprocess for article-assistant, an MCP client for the narration server's runner tools, Audiobookshelf, ntfy, and a file handoff to the agent container.
- **Two images built `FROM agentbox:latest`**, run by compose on the home server.
  - `narration-runner` runs the schedule loop and holds the secrets.
  - `narration-agent` runs a stdlib-only session service. It takes one request at a time from a shared volume, runs the locked-down `claude -p`, and stops the session if its first event lists any tool beyond the six narration tools.

The Mac runs only the narration server from Plan 2, under launchd.

**Tech Stack:** Python ≥3.11 (the agentbox image's Debian Python is 3.11; `tomllib` is in the standard library), `mcp==2.3.0` (its client), `requests`, `defusedxml`, SQLite (standard library), `zoneinfo`; pytest and Hypothesis; Docker compose; agentbox's image and firewall.

**Spec:** `docs/superpowers/specs/2026-10-14-unattended-podcast-pipeline-design.md`. Read with it:
- `docs/superpowers/plans/2026-10-14-unattended-pipeline-roadmap.md`, for spike results S1 (the `claude -p` flags, `--append-system-prompt`, the `--output-format json` fields), S4 (the issue listing) and S2/S3 (added by this plan's PR);
- `docs/narration-server.md`, for the runner tools, the session's MCP config (`"timeout": 900000`) and its two environment variables;
- agentbox's README, "Running a command instead of the agent (compose)".

## Changes this plan makes to the spec (open to veto)

| Change | Why | If it's wrong |
|---|---|---|
| **The runner hands work to the session service through a shared volume** (`/handoff`), not an HTTP call | agentbox's firewall sets `INPUT DROP` and lets in only established traffic, so a service listening in `narration-agent` can't be reached. A file drop needs no inbound rule and no internal network | Replace `handoff.py` and the service's poll loop with HTTP, plus an inbound-port option in agentbox |
| **The schedule is a loop in the runner** (`python -m narration_pipeline schedule`), not cron | The container runs one command (agentbox command mode); a loop is one process with nothing to install, and its next-run time is a pure, tested function | Swap in cron or the host's systemd timer calling `run` |
| **Episode upload and editing go in `pipeline/narration_pipeline/abs.py`**; `publish_audio.py` stays as it is | The runner is a separate project on Linux and can't import the root project. The CLI keeps working (ADR-001) | Move the episode calls into `publish_audio.py` later, if the interactive skill wants descriptions |
| **The morning summary is sent at 07:00**, not at the end of the run | A run ends around 03:00–05:00. Holds still push at once, as the spec says | Change `summary_at` in the config |
| **Container images use `uv sync --frozen`** | Since agentbox #13 the image's 48-hour cooldown makes committed locks stale, and `--locked` fails. `--frozen` never resolves | This takes the "accept it" side of #13's open decision. The other side, `[tool.uv] exclude-newer` per repo, also works |

## Decisions Brian makes before Task 10

1. **How the Mac stays awake overnight** (lid open, on power):
   - **A. `sudo pmset -c sleep 0`** (recommended). The Mac never idle-sleeps on power; the display still sleeps. No test needed.
   - **B. A wake at 00:55 plus a 5-hour keep-awake job.** The Mac sleeps normally otherwise, but this needs one short test, because S2 showed a launchd job five minutes after a wake was missed.
2. **The Mac's checkout:** the recording skill and the Plan 2 LaunchAgent template both use `~/tools/kokoro`, which stopped at PR #5. Plan: update it to `main` and run the server from it (recommended). The other option is to point the template at `~/tools/kokoro-reader`.

## Global Constraints

- **No name contains a TTS model's name.** Package `narration_pipeline`; services `narration-runner` and `narration-agent`; ntfy topic `narration`; launchd label `net.robergb.narration`.
- **Secrets stay where the spec puts them.**
  - `narration-runner` holds the Audiobookshelf key, the ntfy token and the narration runner token.
  - `narration-agent` holds only `CLAUDE_CODE_OAUTH_TOKEN`; it gets each item's agent token in that item's handoff request.
  - No secret goes into an image, a log line, the record, a push, or the repo.
- **Egress** (agentbox `AGENTBOX_*` variables):
  - `narration-agent`: `restricted`, `GITHUB=off`, `BASE=off` (Task 0), and `LAN=<mac-tailnet-ip>:8770`. That leaves exactly `api.anthropic.com` and the Mac.
  - `narration-runner`: `restricted`, `GITHUB=off`, `ALLOW` the source and ntfy hosts, `LAN` the Mac and Audiobookshelf.
- **Compose settings agentbox requires outside its launcher:** `AGENTBOX_NET` set, `cap_add: [NET_ADMIN, NET_RAW]`, `security_opt: [no-new-privileges:true]`, `init: true`.
- **The agent session is exactly the S1 flag set:**
  `--tools "" --strict-mcp-config --mcp-config <file> --allowedTools "<the six>" --permission-mode dontAsk --setting-sources "" --settings '{"disableAllHooks":true}' --no-session-persistence --max-budget-usd <cap> --append-system-prompt <prompt>`
  - Output is `--output-format stream-json --verbose`, so the service can read the `init` event.
  - Environment: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`.
  - No `--bare` (S1: it can't use the subscription).
- **The six tools,** as the `init` event names them: `mcp__narration__get_text`, `mcp__narration__replace_text`, `mcp__narration__record`, `mcp__narration__check`, `mcp__narration__phonemes`, `mcp__narration__finish`.
- **Values from the spec:**
  - cap 5 articles a night, oldest first;
  - start at 01:00; keep trying to reach the Mac until 05:00; no new article after 06:00;
  - after 3 failed nights, hold and push;
  - in scope: edition ≥ 86, or no edition and published on or after 2026-09-08;
  - statuses: `baseline`, `queued`, `in_progress`, `published`, `published_with_notes`, `held`, `failed`.
- **Time zone:** `America/New_York`, from the config, never the container's clock zone.
- **`session_service.py` imports only the standard library,** so the agent image needs no `uv sync`.
- **Red/green TDD, and never commit on red** (`uv run pytest -q > out && git commit`, no pipe). Tests make no network calls. Fixtures come from public pages; published bylines are fine (public figures).

## Review Focus

1. **The Mac sleeps or leaves the tailnet partway through an article.** Expect: the session ends without `finish`, and the runner calls `finalize(force=True)`. The article is marked `failed` and retried the next night. The night carries on, or waits for the Mac until 05:00. It never hangs. (Task 6 test `test_mac_lost_mid_item_fails_item_and_continues`.)
2. **Audiobookshelf takes the upload but the episode appears late, or the run crashes before the episode id is saved.** Expect: the next run finds the episode by its file name and saves its id. It never uploads a second copy. (Task 5 `test_publish_finds_existing_episode_instead_of_uploading`, and the Task 6 property test.)
3. **A session hits its budget or time limit without `finish`.** Expect: `failed`, retried; on the third failed night `held`, with a push saying why. Spend is limited to 3 × the cap. (Task 6 `test_three_failed_nights_hold_and_push`.)
4. **The site changes and the listing has no `TNA_ISSUES` block, or the feed doesn't parse.** Expect: that source's discovery fails loudly, in the summary and a push. Nothing gets queued or baselined from a half-read page. (Task 2 `test_index_without_tna_issues_raises`; Task 6 `test_discovery_failure_is_reported_not_silent`.)
5. **Daylight-saving changes and the container's time zone.** Expect: the run starts at 01:00 New York time on the nights the clocks change, and with `TZ` unset. (Task 7 `test_next_run_is_0100_new_york_across_dst`.)

---

### Task 0: agentbox, `AGENTBOX_BASE=off` (agentbox repo, its own PR)

`restricted` always adds `BASE_DOMAINS` (`statsig.com`, `sentry.io`, `code.claude.com`, `docs.claude.com`). The spec says `narration-agent` may reach only `api.anthropic.com` and the Mac. Add a switch built the way `AGENTBOX_GITHUB` is (agentbox #13).

**Files:**
- Modify: `image/init-firewall.sh`, `README.md` (profile note, the compose section), `env.example`
- Test: `image/tests/test_firewall_base.sh`

**Interfaces:**
- Produces: `AGENTBOX_BASE=on|off`, default `on`. `off` leaves `BASE_DOMAINS` out under `dev` and `restricted`. Any other value is rejected before any request is made. `off` under `open` fails closed, as `AGENTBOX_GITHUB` does. The launcher doesn't forward it; it's for compose.

- [ ] **Step 1: Write the failing test.** Follow `test_firewall_github.sh`: stubs, a pass-through `aggregate`, the real `jq`, `dig` answering 192.0.2.10.
  - With `BASE=off` under `restricted`: no `dig` call for any `BASE_DOMAINS` host, and no `ipset add` of the address `dig` returns for them.
  - With it unset: `dig sentry.io` is called.
  - With `BASE=OFF`: exit non-zero and no `curl` calls.
  - With `BASE=off AGENTBOX_NET=open`: exit non-zero.
- [ ] **Step 2: Run it inside the image** (bash ≥4.4): `docker run --rm -v "$PWD:/src:ro" agentbox:latest bash -c 'cp -R /src /tmp/a && cd /tmp/a && bash image/tests/test_firewall_base.sh'`. Expected: FAIL, because `dig sentry.io` is still called with `BASE=off`.
- [ ] **Step 3: Implement it in `init-firewall.sh`.** `BASE="${AGENTBOX_BASE:-on}"` sits beside `GITHUB`, and is validated and refused under `open` in the same two places. `optional_domains` starts empty when `BASE=off`.
- [ ] **Step 4: Run the whole shell suite in the image.** Expected: every test PASS.
- [ ] **Step 5: Live check on a built image.** `AGENTBOX_NET=restricted AGENTBOX_GITHUB=off AGENTBOX_BASE=off`: `curl https://sentry.io` gives 000 and `api.anthropic.com` gives an HTTP status.
- [ ] **Step 6: Commit and open the agentbox PR.** The README's compose section names the narration-agent combination.

### Task 1: The `pipeline/` project and its config

**Files:**
- Create: `pipeline/pyproject.toml`, `pipeline/uv.lock`, `pipeline/narration_pipeline/__init__.py`, `pipeline/narration_pipeline/config.py`, `pipeline/sources.toml`
- Test: `pipeline/tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Source: id, name, podcast, feed, listing: str | None, extractor, start_edition: int, start_date: date, text_rules: tuple[str, ...]`
  - `@dataclass(frozen=True) class Settings: cap=5, start_at=time(1,0), mac_wait_until=time(5,0), no_new_after=time(6,0), summary_at=time(7,0), tz="America/New_York", failed_nights_to_hold=3, session_budget_usd=2.00, session_timeout_s=5400, gemini_usd_per_check=0.03`
  - `load_sources(text: str) -> list[Source]`
  - `load_settings(text: str) -> Settings`, reading the optional `[settings]` table in the same file

- [ ] **Step 1: Write the failing tests.**
  - `test_loads_the_spec_entry`: `pipeline/sources.toml` loads as one `Source`, with `id == "new-atlantis"`, `podcast == "The New Atlantis"`, `feed == "https://www.thenewatlantis.com/feed"`, `listing == "new-atlantis-issues"`, `start_edition == 86` and `start_date == date(2026, 9, 8)`.
  - `test_unknown_key_is_refused`: an entry with `extractr = "x"` raises `ValueError` naming `extractr`.
  - `test_settings_defaults`: an empty `[settings]` gives `Settings()`.
- [ ] **Step 2: Run them.** `cd pipeline && uv run pytest tests/test_config.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement it.** The project needs `requires-python = ">=3.11"`, with dependencies `mcp==2.3.0`, `requests`, `defusedxml`, and a dev group of `pytest` and `hypothesis`. Parse with `tomllib`, and reject unknown keys. `sources.toml` is the spec's entry, plus an empty `[settings]`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 2: Discovery and the scope rule

**Files:**
- Create: `pipeline/narration_pipeline/discovery.py`
- Create: `pipeline/tests/fixtures/new_atlantis_feed.xml` and `pipeline/tests/fixtures/new_atlantis_issues.html`
- Test: `pipeline/tests/test_discovery.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Candidate: url: str, title: str, published: date | None, edition: int | None, season: str | None`
  - `class DiscoveryError(Exception)`
  - `parse_feed(xml: str) -> list[FeedItem]`, where `FeedItem` has `url`, `title`, `published: date`. Uses `defusedxml.ElementTree`.
  - `parse_issue_index(html: str) -> dict[str, tuple[int, str]]`, mapping each article URL to its issue number and name (`"Summer/Fall 2014"`), read from the inline `const TNA_ISSUES = [...];` array
  - `candidates(feed, index) -> list[Candidate]`: every feed item, plus every index article from an issue at or above the source's `start_edition`, deduplicated by URL, with the edition from the index when it's there
  - `in_scope(c: Candidate, source: Source) -> bool`
  - `LISTINGS: dict[str, tuple[str, Callable[[str], dict]]]`: `{"new-atlantis-issues": ("https://www.thenewatlantis.com/issues", parse_issue_index)}`. A source's `listing` names an entry here, so a second source adds a config entry and, if it needs one, a parser. Nothing else in the package names a site.
  - `discover(source, fetch_text) -> list[Candidate]`: fetches the feed and, when `listing` is set, the listing, then returns `candidates(...)`. Any fetch or parse error becomes `DiscoveryError(source.id, reason)`.

- [ ] **Step 1: Make the fixtures from the live site.** Save `https://www.thenewatlantis.com/feed` as is. From any `/issues/<slug>` page, keep the `TNA_ISSUES` script, trimmed to issues 43 (a double issue), 85 and 86, and drop the rest of the page. Record the fetch date in a comment at the top of each fixture.
- [ ] **Step 2: Write the failing tests.**
  - `test_feed_items`: the fixture's items parse with URL, title and date. "Are We Really on the Path to AGI?" is dated 2026-10-07.
  - `test_index_maps_articles_to_issues`: `index["https://www.thenewatlantis.com/publications/zoning-alone"] == (86, "Fall 2026")`, and a No. 43 article maps to `(43, "Summer/Fall 2014")`.
  - `test_index_without_tna_issues_raises`: a page with no `TNA_ISSUES` raises `DiscoveryError`, and so does malformed JSON in it.
  - `test_web_only_piece_has_no_edition`: the AGI piece's candidate has `edition is None`.
  - `test_scope`, parametrised:
    - edition 86 → True;
    - edition 85 → False;
    - no edition, published 2026-09-08 → True;
    - no edition, published 2026-09-07 → False;
    - no edition and no date → False.
  - `test_index_articles_below_start_are_not_candidates`: no No. 85 article is a candidate unless it's in the feed.
- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Implement it.** Get the array with `re.search(r"TNA_ISSUES\s*=\s*(\[.*?\]);const TNA_INITIAL_SLUG", html, re.S)` and `json.loads`. Anchoring the end on the next statement matches the live page (checked 2026-11-02); a bare `\];` could end inside a string. The fixture test pins it.
- [ ] **Step 5: Run them.** Expected: PASS.
- [ ] **Step 6: Commit.**

### Task 3: The record (SQLite) and its state machine

**Files:**
- Create: `pipeline/narration_pipeline/record.py`
- Test: `pipeline/tests/test_record.py`

**Interfaces:**
- Produces:
  - `STATUSES` (the seven), and `TRANSITIONS: dict[str, frozenset[str]]`:
    - `queued → in_progress`;
    - `failed → in_progress`;
    - `in_progress → published | published_with_notes | held | failed`;
    - `baseline`, `published`, `published_with_notes` and `held` are terminal.
  - `class Record(path: Path)`, opened with `PRAGMA journal_mode=WAL`, one table `articles` keyed by `url`, with the spec's columns plus these:
    - `item_id` (the narration work item);
    - `publish_started` (0/1);
    - `episode_id`;
    - `failed_nights`;
    - `last_error`;
    - `remaining_issues` (JSON);
    - `rounds`;
    - `claude_cost_usd`, `claude_tokens`, `gemini_cost_usd`, `run_seconds`;
    - `created_at`, `updated_at`.
  - Methods:
    - `add_discovered(cands: list[Candidate], source: Source) -> tuple[int, int]` returns (queued, baselined). It inserts new URLs only: `queued` if in scope, else `baseline`.
    - `pick(cap: int) -> list[Row]`: `failed` then `queued`, oldest `published` first, then URL, up to `cap`.
    - `transition(url, to, **fields) -> None` raises `ValueError` for a move `TRANSITIONS` doesn't allow.
    - `in_progress() -> list[Row]`.
    - `row(url) -> Row | None`.
  - `Row` is `sqlite3.Row` (the connection's `row_factory`), read by column name.

- [ ] **Step 1: Write the failing tests.**
  - `test_first_run_baselines_out_of_scope`.
  - `test_rediscovery_changes_nothing`: the same candidates twice gives `(0, 0)` the second time.
  - `test_pick_orders_and_caps`: 7 queued → 5, oldest first, with ties broken by URL.
  - `test_pick_takes_failed_before_queued`.
  - `test_every_allowed_transition`, parametrised over `TRANSITIONS`.
  - `test_disallowed_transition_raises`, for example `published → in_progress`.
  - `test_record_survives_reopen`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement it.** Standard library `sqlite3`, with a `with conn:` transaction per method.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 4: Adapters: fetching, the narration client, ntfy, the handoff

**Files:**
- Create: `pipeline/narration_pipeline/fetch.py`, `narration_client.py`, `push.py`, `handoff.py`
- Test: `pipeline/tests/test_adapters.py`

**Interfaces:**
- Produces:
  - `fetch_text(url) -> str`: GET, 30 s timeout, raises on non-200.
  - `run_article_assistant(url, checkout: Path) -> tuple[str, dict]`: runs `uv run --frozen python article_assistant.py content URL --no-images` and `… metadata --json URL` in `checkout`, with a 120 s timeout each. Returns (markdown, metadata). Raises `FetchError` on a non-zero exit, on output that isn't JSON, or on an empty `title`.
  - `class NarrationClient(base_url, runner_token)` with:
    - `health() -> bool`;
    - `open(text, metadata) -> tuple[str, str]` (item_id, agent_token);
    - `finalize(item_id, force=False) -> dict`;
    - `status(item_id) -> dict`;
    - `download_mp3(item_id, dest: Path) -> Path`.

    It uses the MCP client calls S1 recorded (`mcp.client.Client(streamable_http_client(url, http_client=create_mcp_http_client(headers=…)))`), and plain `requests` for `/health` and `/runner/files/<id>/episode.mp3`. A `ToolError` message becomes `NarrationError(message)`.
  - `class Push(topic_url, token)` with `send(title, body, priority="default") -> None`. Best effort: a 10 s timeout, and failures are logged, never raised. Neither the token nor the body is ever logged.
  - `write_request(handoff: Path, item_id, agent_token, text_rules, budget_usd, timeout_s) -> None`, written atomically (a temp file, then `os.replace`), to `requests/<item_id>.json`. The budget and timeout come from `Settings`, so the runner's config is their one source.
  - `wait_result(handoff: Path, item_id, timeout_s, poll_s=5) -> dict | None`, which reads and removes `results/<item_id>.json`.

- [ ] **Step 1: Write the failing tests.** Use `monkeypatch` for `requests` and `subprocess.run`, and a `tmp_path` handoff.
  - `test_article_assistant_failure_raises`.
  - `test_empty_title_is_a_fetch_error`.
  - `test_push_failure_is_swallowed`.
  - `test_handoff_request_is_atomic`: no partial file is ever visible under `requests/`.
  - `test_wait_result_times_out_to_none`.
  - `test_tool_error_becomes_narration_error`, against a fake MCP client.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement them.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 5: Audiobookshelf episodes (`abs.py`)

**Files:**
- Create: `pipeline/narration_pipeline/abs.py`
- Test: `pipeline/tests/test_abs.py`

**Interfaces:**
- Produces: `class Audiobookshelf(base_url, api_key)` with:
  - `find_episode(podcast: str, filename: str) -> str | None`;
  - `upload(podcast: str, mp3: Path, filename: str) -> None`, the same `/api/upload` call `publish_audio.py` makes;
  - `wait_episode(podcast, filename, timeout_s=600, poll_s=15) -> str`;
  - `set_episode(item_id: str, episode_id: str, title: str, description: str, pub_date: str | None) -> None`;
  - `publish(podcast, mp3, filename, title, description, pub_date) -> str` (the episode id).

  `publish` calls `find_episode` first and uploads only if it returns `None`. The file name is `episode_filename(title, item_id) -> str`: a slug of the title, cut to 60 characters, then `-<item_id>.mp3`. It's deterministic, so a rerun finds its own upload.

- [ ] **Step 1: Write the failing tests** against a fake HTTP layer (a dict of routes to responses).
  - `test_publish_uploads_then_sets_details`.
  - `test_publish_finds_existing_episode_instead_of_uploading` (Review Focus 2).
  - `test_wait_episode_times_out`.
  - `test_filename_is_stable_and_safe`: the same input gives the same name; no `/`, no spaces, at most 60 + 18 characters.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement it.** These Audiobookshelf endpoints are unverified until Step 5. Find the episode with `GET /api/libraries/<lib>/items` (podcast by title), then `GET /api/items/<id>?expanded=1`, matching `media.episodes[].audioFile.metadata.filename`. Set its details with `PATCH /api/podcasts/<itemId>/episode/<episodeId>` and a body of `{title, description, pubDate}`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Live check against Brian's server (Brian, or an agent he gives the key).** `python -m narration_pipeline abs-check --podcast "<a test podcast>"` uploads a one-second MP3, waits for the episode, and sets a test description. Then confirm in the Audiobookshelf web UI that the title, description and date show. Record the server version and any field-name differences in `docs/pipeline.md`, then delete the test episode. If a field name differs, fix `set_episode` and its test before Task 6.
- [ ] **Step 6: Commit.**

### Task 6: The night's run

**Files:**
- Create: `pipeline/narration_pipeline/night.py`
- Test: `pipeline/tests/test_night.py`, `pipeline/tests/test_night_properties.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `@dataclass class Deps: record, sources, settings, fetch_text, article_assistant, narration, handoff_dir, abs, push, now: Callable[[], datetime], sleep`
  - `@dataclass class ItemResult: url: str, title: str, status: str, outcome: str | None, reasons: list[str], cost_usd: float, gemini_usd: float, dry_run_action: str | None`
  - `@dataclass class NightReport: discovered: dict[str,int], results: list[ItemResult], discovery_errors: list[str], mac_unreachable: bool, stopped_at_cutoff: bool`
  - `run_night(deps: Deps, dry_run=False, only: str | None = None) -> NightReport`
- The flow:
  1. **Recover.** For each `in_progress` row (a crash last time), `status(item_id)`:
     - finalized with `publish_started` → `abs.publish` (it finds the upload) → published;
     - finalized without it → publish;
     - otherwise `finalize(force=True)` → `failed`.
  2. **Discover** each source. On an error, add it to `discovery_errors` and move on to the next source.
  3. **Wait for the Mac** (`narration.health()`) every 5 minutes until `mac_wait_until`. If it never answers, stop.
  4. **Process `pick(cap)`** in order, but start no new item after `no_new_after`. For each item:
     1. fetch, then `open`, then `transition(in_progress, item_id=…)`;
     2. `write_request` and `wait_result(timeout=session_timeout_s + 300)`;
     3. `finalize`; a call still open is retried with `force=True`;
     4. by action:
        - `publish`: set `publish_started=1`, download the MP3, `abs.publish(..., description=notes, pub_date=metadata.date_published)`, then `published` or `published_with_notes` (outcome `minor`);
        - `hold`: `held`, and push the reasons;
        - `retry`, or any exception: `failed`, `failed_nights += 1`. At `failed_nights_to_hold`, `held` and push.
     5. Save cost, rounds, remaining issues and run time from the session result and `status`.
- **Dry run:** discovery is recorded, and items run through `finalize`. Nothing is uploaded or pushed, and each processed row goes back to `queued`, with the would-be result in the report. `only=URL` processes just that URL, adding it if it's new.

- [ ] **Step 1: Write the example tests**, with in-memory fakes for every dependency and a fake clock.
  - `test_clean_publishes`.
  - `test_minor_publishes_with_notes`.
  - `test_hold_pushes_reason`.
  - `test_no_finish_fails_and_retries_next_night`.
  - `test_three_failed_nights_hold_and_push` (Review Focus 3).
  - `test_mac_lost_mid_item_fails_item_and_continues` (Review Focus 1).
  - `test_waits_for_mac_until_0500_then_stops`.
  - `test_no_new_item_after_0600`.
  - `test_discovery_failure_is_reported_not_silent` (Review Focus 4).
  - `test_crash_after_upload_recovers_without_second_upload`.
  - `test_dry_run_publishes_nothing_and_requeues`.
  - `test_only_runs_one_url`.
- [ ] **Step 2: Write the property tests** (Hypothesis `RuleBasedStateMachine`). The rules are:
  - run a night, with any outcome per item;
  - crash at any step (the fake raises a `Crash` the test catches);
  - rerun;
  - let time pass.

  The invariants are:
  - no article ever has more than one episode in the fake Audiobookshelf;
  - every row is in exactly one status in `STATUSES`;
  - no night starts more than `cap` items;
  - no row discovered out of scope is ever `queued`, `in_progress` or published.
- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Implement `run_night`.** Take the run lock first: `fcntl.flock` on `<record dir>/run.lock`, non-blocking. A second run exits with "another run is in progress".
- [ ] **Step 5: Run them.** Expected: PASS (Hypothesis with `max_examples=200`).
- [ ] **Step 6: Commit.**

### Task 7: Messages, the schedule and the CLI

**Files:**
- Create: `pipeline/narration_pipeline/messages.py`, `schedule.py`, `__main__.py`
- Test: `pipeline/tests/test_messages.py`, `pipeline/tests/test_schedule.py`

**Interfaces:**
- Produces:
  - `hold_message(row, reasons) -> tuple[str, str]` (title, body);
  - `summary_text(report: NightReport) -> str | None`: `None` when nothing happened;
  - `next_at(now: datetime, at: time, tz: str) -> datetime`;
  - CLI commands:
    - `python -m narration_pipeline run [--dry-run] [--only URL]`;
    - `schedule` (the loop: run at `start_at`, send the summary at `summary_at`);
    - `status` (counts by status, plus the last 10 rows);
    - `abs-check --podcast NAME` (Task 5).
  - Configuration comes from `PIPELINE_CONFIG` (default `/app/sources.toml`) and these environment variables:
    - `NARRATION_URL`, `NARRATION_RUNNER_TOKEN`;
    - `AUDIOBOOKSHELF_URL`, `AUDIOBOOKSHELF_API_KEY`;
    - `NTFY_TOPIC_URL`, `NTFY_TOKEN`;
    - `PIPELINE_DATA` (default `/data`), `HANDOFF_DIR` (default `/handoff`), `ARTICLE_ASSISTANT_DIR` (default `/opt/article-assistant`).

  A missing required variable stops startup with a message naming it.
- [ ] **Step 1: Write the failing tests.**
  - `test_hold_message_names_title_and_reasons`, which asserts that the body has no URL query string and no token.
  - `test_summary_counts`: "2 published, 1 with notes, 1 held, 1 failed", plus each held title. Spend: "Claude $0.41, Gemini about $0.18".
  - `test_summary_none_when_nothing_happened`.
  - `test_next_run_is_0100_new_york_across_dst` (Review Focus 5): from 2026-11-01 00:30 EDT, the next run is 2026-11-01 01:00 EDT, then 2026-11-02 01:00 EST. Also the March 2027 change.
  - `test_missing_env_names_the_variable`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement them.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 8: The session service and the agent prompt

**Files:**
- Create: `pipeline/narration_pipeline/session_service.py` (standard library only) and `pipeline/narration_pipeline/agent_prompt.md`
- Test: `pipeline/tests/test_session_service.py`, `pipeline/tests/test_agent_prompt.py`

**Interfaces:**
- Consumes: the handoff format from Task 4.
- Produces:
  - `EXPECTED_TOOLS: frozenset[str]` (the six);
  - `validate_request(obj) -> Request`. It raises `ValueError` unless:
    - `item_id` matches `^[0-9a-f]{12}$`;
    - `agent_token` matches `^[A-Za-z0-9_-]{43}$` (the format of `secrets.token_urlsafe(32)`);
    - `text_rules` is a list of at most 20 strings of at most 300 characters;
    - `budget_usd` is a number in (0, 5.00] and `timeout_s` an integer in [60, 7200].
  - `mcp_config(narration_url, token) -> dict`: the `docs/narration-server.md` shape, with `"timeout": 900000`.
  - `claude_argv(mcp_config_path, prompt_path, budget_usd) -> list[str]`: the Global Constraints flag set.
  - `check_init(event: dict) -> str | None`: an error unless the event is `system`/`init`, `set(event["tools"]) == EXPECTED_TOOLS`, and the `narration` server is `connected`.
  - `parse_result(event: dict) -> dict`: `{finished_ok, cost_usd, num_turns, terminal_reason, error}` from the final `result` event.
  - `run_session(req, cfg) -> dict`: starts `claude`, reads stream-json line by line, and kills the process group at a failed `check_init` or at `req.timeout_s`.
  - `@dataclass(frozen=True) class ServiceConfig: narration_url: str, prompt_path: Path`, from `NARRATION_AGENT_URL` (for example `http://<mac>:8770/agent/mcp`) and `AGENT_PROMPT` (default `/opt/narration/agent_prompt.md`). `run_session(req, cfg)` takes its budget and timeout from `req`.
  - `serve(handoff: Path, cfg) -> None`: polls `requests/` every 2 s, moves a request to `running/`, writes `results/<id>.json`, and removes the request. One session at a time.
- `agent_prompt.md`:
  - the recording skill's judgment sections (Steps 2 and 5, the common TTS issues, phonetic overrides, general principles), rewritten to name the six tools and no shell commands;
  - all of `docs/narration-server.md` "What an agent session must do";
  - the source's `text_rules`, added to the `-p` prompt per item.

- [ ] **Step 1: Write the failing tests.**
  - `test_check_init_refuses_an_extra_tool`: `Bash` among the tools gives an error naming `Bash`.
  - `test_check_init_refuses_a_missing_tool`.
  - `test_check_init_refuses_a_disconnected_server`.
  - `test_run_session_kills_on_bad_init`: a fake `claude` script on `PATH` prints an init event with `Bash` and then sleeps. Expect the process to be gone within 5 s, and `finished_ok` False with error "tool check failed: Bash".
  - `test_run_session_timeout_kills`.
  - `test_validate_request_refuses`, parametrised over: a path as `item_id`; a token with `/`; 21 rules; a 301-character rule; a budget of 50; a timeout of 10.
  - `test_service_imports_only_stdlib`: parse the module's imports with `ast`; each must be in `sys.stdlib_module_names`.
  - `test_prompt_names_every_tool_and_no_shell`: every name in `EXPECTED_TOOLS` (without the prefix) appears; none of `uv run`, `python `, `.py`, `cd ` appear.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement them, and write the prompt.** Use `subprocess.Popen(..., start_new_session=True)` and `os.killpg` on timeout or a failed check. The environment is the Global Constraints four plus `PATH` and `HOME`, and nothing else from the service's environment except `CLAUDE_CODE_OAUTH_TOKEN`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 9: Images, compose, and the containment checks

**Files:**
- Create: `deploy/narration-runner/Dockerfile`, `deploy/narration-agent/Dockerfile`, `deploy/compose.yaml`, `deploy/runner.env.example`, `deploy/agent.env.example`, `docs/pipeline.md`
- Test: `pipeline/tests/test_deploy_files.py` (a static check of the compose file); the live checks are below.

**Interfaces:**
- `narration-runner`:
  - `FROM agentbox:latest`;
  - copies `pipeline/` to `/app` and runs `uv sync --frozen`;
  - clones article-assistant at `ARG ARTICLE_ASSISTANT_REF=7fc4055` into `/opt/article-assistant` and runs `uv sync --frozen` there;
  - `CMD ["uv","run","--frozen","python","-m","narration_pipeline","schedule"]`;
  - volumes: `narration-data:/data` and `narration-handoff:/handoff`.
- `narration-agent`:
  - `FROM agentbox:latest`;
  - `ARG CLAUDE_CODE_VERSION`, set by compose to the version Task 9 Step 3 passes on, installed with `npm install -g`;
  - `ENV DISABLE_AUTOUPDATER=1`;
  - copies `session_service.py` and `agent_prompt.md` to `/opt/narration`;
  - `CMD ["python3","/opt/narration/session_service.py"]`;
  - volume: `narration-handoff:/handoff` only.
- Both services have `init: true`, the two capabilities, `no-new-privileges`, `restart: unless-stopped`, `TZ=America/New_York`, and the egress from Global Constraints.
- Secrets come from `env_file` paths outside the repo (`/srv/narration/runner.env`, `/srv/narration/agent.env`, mode 600). The `.example` files list the variable names only.

- [ ] **Step 1: Write the failing static test.** `test_compose_has_agentbox_settings` checks that both services have `init: true`, both capabilities, `no-new-privileges`, and `AGENTBOX_NET=restricted`. The agent also needs `AGENTBOX_GITHUB=off`, `AGENTBOX_BASE=off`, and no `env_file` besides `agent.env`. Neither service sets `ports:`. Parse the file with `pyyaml`, added to the dev group.
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Write the Dockerfiles and the compose file.** Build both images on the home server, after building `agentbox:latest` there from agentbox `main`.
  - **Brian makes the secrets:**
    - an ntfy topic `narration` with its own token;
    - `claude setup-token` for `CLAUDE_CODE_OAUTH_TOKEN` (about a year's life, per agentbox's notes);
    - the runner token, the same value as the Mac's `~/.config/narration/runner-token`.
  - **Login expiry:** `docs/pipeline.md` records the Claude token's creation date. Brian sets a reminder for 11 months on. A session that fails to log in shows as `failed` nights and then a hold, so a lapsed token is noticed within three nights in any case.
- [ ] **Step 4: Run the containment checks** on the home server, and record each output in `docs/pipeline.md`:
  - **Egress from `narration-agent`:** `curl` to `https://example.com`, `https://sentry.io` and `https://api.github.com` all fail. `https://api.anthropic.com` returns an HTTP status, and `http://<mac>:8770/health` returns `{"ok":true}`.
  - **A real session refuses built-ins.** Run `tests/narration/smoke_claude.sh`'s prompt through the session service, with fake engines on the Mac: no probe succeeds, and the session's `init` passes `check_init`.
  - **The service refuses bad requests.** Write a request with `item_id` `../../etc` and another with a malformed token. Each gets a result with `finished_ok: false` and a validation error, and `claude` never starts (`ps`).
  - **The narration tokens hold.** The Plan 2 token checks pass from inside `narration-runner` (`curl` without a token → 401; a runner tool with an agent token → 401).
  - Already covered by Plan 2's tests, not repeated here: the word-preservation and `replace_text` property tests, and the planted-instructions article (`tests/narration/test_containment.py`).
- [ ] **Step 5: Run it.** The static test PASS, and all four checks pass.
- [ ] **Step 6: Commit,** with `docs/pipeline.md` covering deploy, operate (`status`, a dry run, reading the record), and rollback (`docker compose down`; the record and work folders stay).

### Task 10: The Mac (Brian)

Uses the two decisions above.

- [ ] **Step 1: Update the checkout.** `cd ~/tools/kokoro && git pull && uv sync --inexact`, then install the spaCy model as the README says (decision 2).
- [ ] **Step 2: Keep the Mac awake.** Decision 1: `sudo pmset -c sleep 0`, or the wake plus the keep-awake job (then run that job's short test before relying on it).
- [ ] **Step 3: Install the LaunchAgent** from `deploy/net.robergb.narration.plist`, following `docs/narration-server.md` "Install on the Mac": the tailnet IP, the runner token file, and the Gemini key in `.env`. Check from the home server with `curl http://<mac>:8770/health`.

### Task 11: Acceptance (Brian listens)

- [ ] **Step 1: Dry run.** `docker compose run --rm narration-runner uv run --frozen python -m narration_pipeline run --dry-run --only <a No. 86 article URL>`. Expected: the report shows `would publish` with notes; the row is `queued` again; Audiobookshelf is unchanged; no push.
- [ ] **Step 2: A real run of the same article.** The same command without `--dry-run`. Expected: one new episode in "The New Atlantis", with its title, description, notes and date; the row is `published` or `published_with_notes`.
- [ ] **Step 3: Brian listens** to the episode and reads its notes.
- [ ] **Step 4: Turn on the schedule** with `docker compose up -d`. After the first night, the 07:00 summary arrives, and `status` shows the night's rows.
