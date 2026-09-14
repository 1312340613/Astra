# Skill learning and manual review

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/skill-learning.md)

Astra can save reusable methods directly as skills while working. It does not
require a candidate, an experiment and an activation decision for every lesson.
The model chooses when a lesson is worth saving; it is not required to create
one after every conversation.

## Use the library

```text
/skills
/skills show <name> [file]
/skills create <name> <description>
```

Skills contain `SKILL.md` and optional `references/`, `templates/`, `scripts/`
or `assets/`. The model saves its own summaries with `skill_manage(origin="auto")`
and uses `origin="user"` for content added at the user's request. Only the catalog
is injected; full instructions are read on demand. Packaged core rules have
their own [loading contract](runtime/core-rules.md).

## Skill ownership

| Source | Meaning | Included in `/learn review` |
| --- | --- | --- |
| `auto` | A method summarized and maintained by the model | Yes |
| `user` | A skill added, installed or edited on the user's behalf | No |
| `builtin` | Packaged core rules | No |

Automatic skills normally live in `.astra/skills/learned/<name>/`. New user skills
go under `user/`; existing categories are retained. `AGENT_SKILLS_PATH` can select
another library. Ownership comes from the writer's provenance record, not a
claim in the skill text or the directory name. Unknown ownership is protected
as user content. `/skills` shows ownership and category.

Skills should explain when to use a method, its steps, limitations and source.
They are listed in the normal skill catalog and read through `skill_view` when
relevant. A recorded experience does not establish that its commands still work
on another machine or version.

## Review a batch

```text
/learn
/learn review
/learn history
/learn history <run-id>
/learn undo <run-id>
```

`/learn review` checks only automatic skills and may keep, rewrite, merge or
archive them. User skill content and descriptions are excluded from its review
request. It reviews text and supplied source material; it does not run commands
from the skills or claim that their procedures passed a live test.

Each invocation sends one bounded model request: at most four skills and 24,000
input content/source characters, with a 90-second timeout and 4,096 output-token
limit. A saved cursor tracks coverage in stable order. Run the same command
again to inspect the remaining skills, even in another session; after a complete
pass, later reviews can revisit the library. Oversized entries are reported as
not checked rather than silently counted as reviewed.

Pinned, other-workspace, other-platform and externally edited skills are
protected. Only fully included automatic skills can be modified or merged. Invalid,
incomplete or truncated model output leaves the batch unchanged and does not
advance its cursor. Empty libraries do not call the model. There is no automatic
retry, timer, idle review or catch-up job on startup.

Changes keep their previous versions and reasons under `.astra/skills-learning/`,
outside the catalog. Archived skills remain available in that history. A journal
recovers interrupted multi-file writes. If records cannot be read, `/learn`
reports the error and the LEARN row shows `? / CHECK`; ordinary chat continues
without replacing damaged records. A library lock prevents
concurrent review writes; content checks protect files changed after review
started. Undo refuses to overwrite later edits. Closing the terminal stops
unfinished maintenance, while already committed changes remain in history.
Ctrl+C or a new message cancels review. A short file commit already in progress
settles before cancellation returns; inspect history for its outcome.

`/learn mode off` disables direct automatic saving. `/learn mode review` enables
it; the legacy name `review` does not turn on scheduled maintenance. Explicit
`/learn review` remains available while saving is off.

## Migrate an older library

```text
/learn legacy pending
/learn migrate
```

Migration backs up the old SQLite store and imports eligible automatic skill
summaries using the existing file transaction and undo mechanism. It records
original IDs and destinations, so repeating migration does not duplicate skills.
Name collisions and old patches cannot overwrite user skills. Original database
records remain available as history.

Observations and environment notes stay in the historical archive rather than
becoming skills. They can be retrieved through
[local history search](local-history-retrieval.md). A historical candidate count
therefore need not equal the automatic-skill count or the review batch size.

## Maintenance references

The implementation is in `agent/runtime/skill_learning.py`, `skill_curation.py`,
`skill_provenance.py`, `skill_migration.py` and `agent/cli/learning_commands.py`.
Tests cover ownership, cursor progress, invalid output, concurrent changes,
migration and undo. The optional `scripts/skill_curation_acceptance.py` exercises
copies of historical data; keep source records and model outputs out of Git.
