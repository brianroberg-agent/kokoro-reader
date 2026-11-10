# Unattended article-to-podcast pipeline: design

**Status:** draft for Brian's review, 2026-10-14. Not approved yet. Nothing gets built until it is.
- **Revision 2 (same day):** the agent's tools become one MCP server (Brian's call).
- **Revision 3 (same day):** Brian chose option R on the PR. The runner and the agent run in a container on the home server, and the Mac runs only the MCP server. So v1 includes roadmap item 4, and there's no second Mac account.

**Decided with:** Brian, by voice and on this PR, 2026-10-14. The assistant made the calls under "Decisions made here (open to veto)".
**Relationship to ADR-001:** an agent still orchestrates each recording and makes the judgment calls. What changes is that the session runs on a schedule, with nobody at the keyboard. See `docs/adr-002-unattended-runs.md`.

## Goal

New articles from the sources Brian follows appear as checked episodes in his Audiobookshelf podcast feed, with nothing for him to do. He hears about an article only when it needs his judgment.

## What Brian decided

- **Intake is automatic from sources he follows.** He doesn't pick individual articles.
- **The first source is The New Atlantis, every article.** Back issues are out of scope. On the first run, everything older than the **Fall 2026 edition (No. 86)** counts as back catalog. The Fall 2026 edition itself gets recorded.
- **Nothing is New Atlantis-specific beyond its config entry.** v1 has one source, but no choice may make a second source harder than adding an entry, plus an extractor if the site needs one.
- **The work runs overnight on the MacBook,** at home and on power. Episodes don't need to appear the night an article is posted.
- **Quality rule:**
  - When only minor issues remain (for example a mispronounced name), the episode publishes, with the issues listed in its notes.
  - When content is missing or garbled, it's held and Brian gets a push saying why. (Since 2026-11-10, only a gross error holds: a sentence or more missing or out of place, invented text, or a stretch a listener can't follow; see the next note.)
- **Notices come by push.** Holds and repeated failures notify at once. A summary of the night's results arrives each morning.
- **The agent runs on Brian's Claude subscription.**
- **The agent reaches the work through an MCP server, not shell commands** (revision 2). The skill stays as its guide to judgment.
- **No name includes a TTS model's name,** because the model will likely change (his PR comment). That applies to the Mac account, the MCP server, the launchd job and the container. The repo keeps its name.
- **Approach: a nightly job, with the agent off the laptop from v1** (option R, his PR comment). The runner and the agent run in a container on the home server. The Mac runs only the narration server, as Brian's own user, with no second account.
- **Cost is measured per article.** The cap of 5 articles a night is a starting point, tuned from real numbers once it runs.
- **Property-based tests are in scope** (see "Testing").

## Not in v1

- More than one source.
- Testing new TTS models (roadmap item 3).
- Removing the Gemini dependency, and TTS models that can redo just a passage instead of the whole recording (future work).
- A line in the morning briefing. The morning push covers the summary.
- Back issues.

## Architecture

Each night a scheduled runner, which is plain code, takes these steps:

1. **Discover.** For each source, list candidate articles and add any new in-scope ones to the record as `queued`.
2. **Pick.** Take up to 5 `queued` articles, oldest first.
3. **Per article:**
   1. **Fetch (runner).** Run article-assistant to get the article's text and metadata, then open a work item on the narration server.
   2. **Record (agent session).** One headless Claude Code session prepares the text, records, checks and fixes, following the skill. It does all of this through the narration server's tools, and ends by calling `finish`.
   3. **Gate (narration server).** Run the word-preservation check and the claim checks (see "Quality gate").
   4. **Publish (runner).** Have the server convert to MP3 with tags, then upload it to Audiobookshelf with episode notes, and record the outcome.
4. **Notify.** Send pushes for holds and for repeated failures, plus one morning summary if anything happened.

### The narration server (MCP)

One small Python MCP server, named `narration`, wraps the toolkit's functions. It owns each article's work folder, and the agent never handles a path. It has two sets of tools, each behind its own bearer token.

**Agent tools.** These are the only tools the agent session has:

| Tool | Does |
|---|---|
| `get_text()` | Returns the prepared text and the article's metadata |
| `replace_text(old, new)` | An exact-match edit of the prepared text, like Claude Code's Edit tool |
| `record(voice?, speed?)` | Generates the audio from the prepared text, then returns its duration. **It refuses a fifth call per article.** |
| `check()` | Runs the Gemini verification and returns the report. The server keeps every report. |
| `phonemes(word)` | Looks up a pronunciation with misaki's G2P, for phonetic overrides |
| `finish(outcome, remaining_issues, substitutions)` | Ends the session with a typed result. The input is validated on the spot, so a malformed result is an error the agent can fix while it's still running. |

**Runner tools.** The agent never sees these:
- `open(article_text, metadata)` creates a work item.
- `finalize(id, tags)` runs the gate, then converts to MP3.
- `download(id)` is a plain HTTP route for the MP3.
- `status(id)`.

**The server holds the Gemini key.** It doesn't hold the Audiobookshelf key, because the runner publishes.

**The CLIs stay.** `text_to_speech.py`, `verify_audio.py` and `convert_audio.py` remain independently callable, as ADR-001 requires. The server calls the same functions. Brian's interactive sessions can use the same server, so there's one interface rather than two.

### The agent session

- `claude -p` runs with only the `narration` server's agent tools. Bash, file tools and web tools are all switched off.
- *Unverified:* the exact flags for "only these MCP tools, nothing built in." The expectation is `--strict-mcp-config` with `--mcp-config` plus allow and deny lists. It gets tested first in the plan.
- The prompt names the article, and the skill tells the agent how to judge.

**The prompt-injection risk remains.** The article's text reaches the agent through `get_text()`. The protections against it:
- the agent has no tools beyond the six above;
- each tool's arguments are typed, and no tool takes a path;
- the word-preservation check runs before anything publishes.

## Where things run (option R, chosen 2026-10-14)

The narration server runs on the Mac, because MLX needs Apple Silicon. The runner and the agent run off the laptop.

- **The Mac runs only the narration server.** It runs as Brian's own user, as a launchd LaunchAgent (`net.robergb.narration`). It's bound to the Mac's tailnet address, with the two tokens. Brian's voicemode server already works this way.
- **No second Mac user is needed.** The account existed only because an LLM would be reading untrusted text on the Mac. Here the Mac runs deterministic code that takes typed arguments and never reads instructions.
- **The runner and the agent run in two containers on the home server,** joined by a private internal network. Both are built on agentbox's image and egress firewall.
  - **`narration-runner`** holds the Audiobookshelf key, the push token and the runner token. It may reach the source sites (for fetching), Audiobookshelf, the push server and the Mac's narration endpoint.
  - **`narration-agent`** holds only the Claude login (a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, as agentbox already uses) and the agent token. It may reach only `api.anthropic.com` and the Mac's narration endpoint. A small session service in it accepts one call from the runner, "run the session for work item N." It validates the id, runs `claude -p`, and returns the outcome and cost. Nothing else reaches it.
  - **Why two containers, not one with two Linux users.** In one container, the runner would have to run as root to start sessions as another user. The two containers' network permissions would also merge. Split, a hijacked agent sits in a container with no secrets beyond its own Claude login, and two places it can reach.
- **The home server is always on.** Discovery and the record can run any time; only recording needs the Mac to be awake.
- **This container is not the existing assistant's.** Revision 1 set aside "an agent on the home server" because that assistant holds unrelated credentials. That reason applies to running inside the assistant, not to the host.

**What this costs:**
- **Two machines and a tailnet link.** If the Mac isn't reachable, recording waits until the next night.
- **Brian deploys** two compose services on the home server, plus the launchd job on the Mac.

**Considered and set aside:**
- **L: the runner and the agent on the Mac,** under a separate macOS user. It's simpler, with one machine, but it needs a second Mac account and leaves roadmap item 4 for later.
- **The shelved `agents` VM,** because reviving it means taking RAM back from the home server's main VM.
- **The existing assistant's container,** because it holds unrelated credentials.

## Sources

Each source is an entry in `sources.toml`:

```toml
[[source]]
id = "new-atlantis"
name = "The New Atlantis"
podcast = "The New Atlantis"        # Audiobookshelf podcast the episodes go to
feed = "https://www.thenewatlantis.com/feed"
listing = "new-atlantis-issues"     # optional extra discovery, named per site
extractor = "new-atlantis"          # article-assistant mode
start = { edition = 86, date = "2026-09-08" }
text_rules = []                     # source-specific preparation notes for the agent
```

- **Discovery** reads the feed, plus the optional per-site listing. The New Atlantis feed returned only the 10 most recent items on 2026-10-14, and nine of them came out together on 9/8. An issue with more than 10 articles would overflow the feed, so the listing (the site's `/issues` page, which shows "No. 86 Fall 2026") fills the gap. *How exactly to read an edition's article list from that page gets settled in the implementation plan, against the live page.*
- **In scope** means the article's edition is 86 or later. A piece without an edition (web-only) is in scope if it was published on or after the start date.
- **First run:** everything discovered that's out of scope is recorded as `baseline` (seen, never recorded).

## State and work folders

**The record** is one SQLite file, kept by the runner, with one row per article, keyed by its URL. Each row holds:
- the source, title, edition and publication date;
- its status: `baseline`, `queued`, `in_progress`, `published`, `published_with_notes`, `held` or `failed`;
- the number of failed nights and the last error;
- the remaining issues, and the number of rounds;
- the Claude tokens and cost (from `claude -p --output-format json`), an estimated Gemini cost, and the run time;
- the Audiobookshelf episode id, and timestamps.

**Each article gets a work folder on the Mac,** kept by the narration server and kept after the run:
- `source.md` and `metadata.yaml`, as fetched;
- `prepared.md`, the text after the agent's edits;
- `report-<n>.txt`, one per check;
- `result.json`, the `finish` payload;
- the final WAV and MP3.

These folders later become the test set for comparing TTS models.

## The agent session's contract

**Input:** a work item id, and the skill, adapted so its steps name the narration tools instead of shell commands.

**Output:** a `finish` call:

```json
{
  "outcome": "clean | minor | hold",
  "remaining_issues": [{"kind": "mispronunciation", "where": "12:40", "note": "Pengelley"}],
  "substitutions": [{"from": "XIV", "to": "the Fourteenth", "why": "roman numeral"}]
}
```

- **At most 4 rounds per article.** `record` enforces this itself, so the limit doesn't rest on the prompt.
- **`hold` is for missing or garbled content.** `minor` is for issues the skill would "likely ignore" or couldn't fix. **Changed 2026-11-10 (Brian):** "Let's only block publication for gross errors like a sentence (or more) of missing or misplaced text, hallucinated text, or degradation of intelligibility." and "There should still be a correction pass to attempt to fix smaller problems, but smaller problems shouldn't block publication." So `hold` is only for those; everything smaller is `minor` once a fix was tried. The gate's own checks still block at any size.
- **Every word change goes in `substitutions`.** The gate rejects any undeclared change.
- **A session that ends without calling `finish`** is a failure.

## Quality gate (narration server, deterministic)

1. **Claim checks.** The server compares the agent's claim with its own records:
   - `finish` must come after a `check` that ran on the latest recording;
   - `clean` or `minor` is refused if that check's deterministic pre-check flagged truncation or long silences.
2. **Word-preservation check.** This enforces the skill's rule, "never change what the article says." Both texts are reduced to plain words. In `prepared.md`, a `[word](/phonemes/)` override is read as its displayed word. Allowed punctuation and pause changes are set aside, and each declared substitution is reversed. The two word sequences must then match exactly; any difference holds the article. This also stops a hijacked session from adding words of its own.
3. **Outcome:**

   | Outcome | Action |
   |---|---|
   | `clean` | Publish. |
   | `minor` | Publish, with the issues in the episode notes. |
   | `hold` | Hold, and push the reason. |
   | A gate failure | Hold. |
   | A tool error, network error, or no `finish` call | Mark `failed` and retry the next night. After 3 failed nights, hold and push. |

## Publishing

- **The episode title, notes and date come from the metadata.** This needs article-assistant #3 (the New Atlantis description and publication date).
- **`publish_audio.py` must learn to set an episode's description.** Today it uploads only a file and a title.
- **Publishing is idempotent.** It only runs from `in_progress`, and the episode id is saved, so a rerun can't double-publish.

## The nightly job

- **It starts at 01:00,** from cron in the container. Discovery runs even when the Mac is unreachable. Recording retries reaching the Mac until 05:00, then waits for the next night.
- **One run at a time,** enforced by a lock.
- **No new article starts after 06:00.**
- **Dry-run mode** does everything except publish and push.
- **The pushes** use a dedicated push topic and token for this job, not one shared with other services.

## Testing

**Built test first throughout.** Pure logic is kept apart from I/O, so it's tested without network calls. The narration tools are plain functions, so they're tested the same way.

- **Example tests, with saved fixtures** (a feed sample and an issue-page sample):
  - discovery parsing and the in-scope rule;
  - the first-run baseline;
  - every state transition;
  - the cap and the ordering;
  - `finish` validation;
  - the claim checks;
  - outcome mapping;
  - the summary text;
  - the per-tool round limit.
- **Property tests (Hypothesis):**
  - No article is ever published twice, across any sequence of runs, crashes and reruns.
  - Every article processed in a run ends in exactly one status.
  - A night never starts more than the cap.
  - Nothing out of scope is ever queued.
  - **Word preservation:**
    - for any article text and any mix of allowed edits plus declared substitutions, the check passes;
    - for any undeclared word change (insert, delete or replace), it fails.
  - `replace_text` can't touch anything outside its own work item, whatever its arguments.
- **Containment tests:**
  - A real headless session is asked to run a shell command, read a file and fetch a URL. All three must be refused.
  - A planted test article carries hidden instructions. Any text it gets inserted must fail the word check.
  - From inside `narration-agent`, any host other than its two allowed destinations can't be reached. Its session service refuses anything but a valid work id.
  - The narration server refuses a call without a token, and refuses a runner tool called with the agent token.
- **Acceptance:** one real article end to end in dry run, then published. Brian listens to it before the schedule is turned on.

## Order of work

0. **Prerequisites:**
   - this repo's #6 (rename the skill to `recording-article-audio`);
   - article-assistant #3 (the New Atlantis description and publication date);
   - **agentbox, three small changes:**
     - let the entrypoint run a given command, not only its tmux start script;
     - a switch to leave GitHub out of the `restricted` profile;
     - uv in the image (agentbox #6).
   - For an unattended service, also pin the Claude Code version and turn off auto-update. agentbox #10 documents why auto-update fails in that image anyway.
1. **v1, as above,** with the agent in the container on the home server.
2. **Cost review** after a couple of weeks, to set the cap from the recorded numbers.
3. **A model-testing harness** that replays saved work folders through a candidate TTS or checking model, behind the same `record` and `check` tools. This also covers this repo's #3 (local models for the check).
4. ~~Move the agent off the laptop~~: done in v1 (option R).
5. **More sources.**

Future work Brian named: remove the Gemini dependency, and use newer TTS models that can redo a single passage instead of the whole recording.

## Decisions made here (open to veto)

| Decision | Why | If it's wrong |
|---|---|---|
| The runner fetches and publishes; the agent doesn't | Keeps the Audiobookshelf key, the push token and open web access out of the agent's reach | Little lost; those steps simply sit in code |
| Two containers (runner and agent), not one with two users | The agent's container has no other secrets and two egress destinations; the runner never needs root | One more small service, the agent's session endpoint |
| One MCP server, with separate agent and runner tool sets and tokens | The agent can't open work items or download files; one process to run | Splitting it into two servers later is mechanical |
| The server name is `narration`, the containers are `narration-runner` and `narration-agent`, and the launchd label is `net.robergb.narration` | Brian's rule: no model name | A rename |
| SQLite for the record | One file; safe updates mid-run | A JSON file would also work; it's a small swap |
| Code lives in this repo, in a `pipeline/` folder | Uses the toolkit directly, and keeps article-assistant light (ADR-001) | Moving it to its own repo later is mechanical |
| Web-only pieces count from the start date | Brian asked for every article; the edition rule can't cover pieces without an edition | A web-only piece could be recorded that he'd have skipped. He'd see it in the feed and the summary |
| Up to 4 rounds per article | The skill expects 2–4 for a long article | Too low means more holds; too high costs more. Both show up in the recorded numbers |
| Start at 01:00, no new article after 06:00 | Overnight, and done before he's up | Change the schedule |

## Open questions to settle early in the plan

- **What are the exact `claude -p` flags** that give a session only the narration server's tools, with every built-in tool off? Verify before building on it.
- **Does a scheduled wake work on this MacBook when it's closed and on power?** If not, it has to be left open and awake overnight (for example with `caffeinate`).
- **Headless Claude login inside the container.** agentbox already uses `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. Its notes say this works with a Max subscription and lasts about a year. Confirm it here, and add a reminder before it expires.
- **Does MLX get the GPU when the narration server runs as a LaunchAgent** rather than from a terminal?
- **How does The New Atlantis's `/issues` page mark which articles belong to an edition?** This decides how the listing is read.
