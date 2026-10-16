---
name: nfl-audio-parser
description: Transcribe a bounded local audio segment, load the authoritative NFL schedule, and extract reviewable NFL picks with exact speaker quotes, timestamps, and conviction evidence.
---

# NFL audio parser

Before acting, read and follow the complete canonical workflow at:

`../../../.claude/skills/nfl-audio-parser/SKILL.md`

That skill is shared by Claude Code and GitHub Copilot. Perform the extraction
in the current Copilot session and follow every safety, schedule-loading,
quotation-verification, conviction, and review-only requirement in the
canonical skill.

The canonical workflow accepts either a local audio path or one public YouTube
video URL as the source. YouTube media remains temporary, and only the bounded
segment is uploaded for transcription.
