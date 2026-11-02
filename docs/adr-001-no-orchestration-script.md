# ADR-001: No Orchestration Script — Agent-Driven Workflow

**Status:** Accepted
**Date:** 2026-03-17
**Context:** Architecture decision for the kokoro TTS toolkit

## Decision

The article recording workflow is orchestrated by a Claude Code agent using a project skill (`.claude/skills/recording-article-audio/SKILL.md`), not by a deterministic Python script. The individual tools (`text_to_speech.py`, `verify_audio.py`, `convert_audio.py`) remain independently callable commands. Article content is fetched from a separate project (`article-assistant`) which interoperates via plain text on stdout.

## Context

This project produces audio recordings of articles. The end-to-end workflow involves: fetching an article's text and metadata, preparing it for TTS, generating audio, verifying the audio against the source text, iteratively fixing issues, and converting the final result to MP3 with ID3 tags.

We considered building a `record_article.py` wrapper script that would orchestrate this entire pipeline in a single command (`uv run python record_article.py URL OUTPUT_DIR`). The script would call article-assistant via subprocess, run TTS generation, invoke Gemini verification, and convert to MP3.

## Why We Decided Against the Wrapper

**The iteration loop requires judgment, not automation.** The generate-verify-fix cycle is not deterministic. The number of iterations varies (1 for short articles, 3-5 for long ones). More importantly, the *assessment* of Gemini's verification report requires judgment calls:

- Gemini sometimes flags normal speech variation as mispronunciation (e.g., soft 't' in "Pontiac", 'z' sound in "crusade" which is actually correct). A script would either over-correct or need complex heuristics.
- Some proper nouns resist phonetic overrides and need word substitution instead — a decision that depends on context.
- The choice between fixing an issue and accepting it depends on how prominent the word is in the article.

These are exactly the kinds of decisions an agent with domain context handles well, and that a deterministic script handles poorly.

**The tools already compose cleanly.** The article-assistant outputs plain text and YAML. The TTS toolkit consumes plain text. There's no shared state or complex API boundary — just text in and text out. An orchestration script would add a layer of indirection without reducing the actual work.

**Keeping projects separate preserves independence.** The article-assistant is lightweight (requests, beautifulsoup4, markdownify). The TTS toolkit is heavy (mlx, torch, spacy, google-genai). Merging them would force anyone who just wants article extraction to install ~500MB of ML dependencies.

## What We Did Instead

1. **Extracted `generate_audio()` from `text_to_speech.py`** — a callable function that handles the full text-to-audio pipeline (markdown cleaning, TTS preparation, section splitting, chunk generation, concatenation). This makes the TTS generation independently callable from any Python code, without needing to go through the CLI.

2. **Wrote a detailed project skill** (`.claude/skills/recording-article-audio/SKILL.md`) that documents the complete workflow, including both project locations, exact commands, metadata format, common issues and fixes, and assessment guidance for the verification report.

3. **The agent remains the orchestrator.** It invokes the skill, makes judgment calls during iteration, and adapts to each article's specific challenges.

## Consequences

- New articles require an interactive session with the agent — there's no batch processing capability.
- The workflow knowledge lives in the skill document rather than in code, which means it evolves through documentation updates rather than code changes.
- If batch processing becomes needed in the future, `generate_audio()` is available as a building block.
