# On-demand local history retrieval

## Intended behavior

The model can consult previous conversations and saved observations when they
help with the current task, even when the user has not explicitly asked it to
remember. It requests this information through `session_search`; the observation
archive is not added to the prompt automatically.

Mac installations use existing local SQLite files. Windows installations can
continue using Hindsight alongside this local lookup. This change adds no
service, timer, embedding model, or background summarizer.

## Storage and compatibility

- Normal TUI conversations continue using the existing Session Recall writer
  and `.astra/sessions.db`. Existing isolation and API access rules remain.
- Historical observations are read in place from `.astra/learning.db`, or
  `AGENT_LEARNING_PATH`. Only `observation` entries in `pending` or `applied`
  status are eligible. Rejected, superseded and rolled-back entries are excluded.
- Original IDs, timestamps, source session IDs and evidence are preserved.
  No migration rewrites, copies into core memory, or approval-state changes occur.
- Automatically learned skills and user skills stay in their own libraries.
  The observation search does not modify skills or participate in `/learn review`.
- Hindsight configuration, recording and recall remain unchanged.

Reading the existing archive avoids both a second database to synchronize and
accidental promotion of historical notes into current, authoritative memory.

## Tool contract

- `session_search(query="ComfyUI pip")` keeps its existing conversation results
  and adds a separately counted `observations` section.
- `source_type="learning"` searches or browses only saved observations. Existing
  `astra`, `hermes`, and `api` filters keep their previous session-only meaning.
- `record_id="learning:lr_..."` expands an observation returned by a prior call.
  This mode does not initialize or search the session database.
- Search returns bounded excerpts. Expansion returns bounded original content
  and evidence, with an explicit truncation indicator.
- Empty or unavailable archives are distinguished. A failure in one archive
  does not conceal usable results from the other archive.

Observation matching uses the existing bounded lexical matcher, including
Chinese terms and English identifiers. It is keyword retrieval, not semantic
search. Session queries retain their existing FTS5 behavior; observation queries
use plain keywords rather than the FTS5 boolean language.

## Historical evidence boundary

Every observation response labels the material as historical, potentially stale
and unverified. An old `applied` status describes the former learning workflow;
it does not verify the content today. Retrieved text is data, not instructions
or authorization to execute its procedures. Current user instructions and live
evidence take precedence. Missing workspace/platform metadata remains unknown.

## Verification

Tests cover mixed-language matching, source filters, bounded expansion,
read-only/absent/corrupt/locked archives, excluded statuses and malformed rows,
and preservation of session browse/search/scroll behavior. Acceptance uses a
copy of the existing archive and a bounded real-model retrieval request.
Existing Session Recall, Context Index and release-gate regressions are run.
Mac testing does not establish native Windows or live Hindsight acceptance.
