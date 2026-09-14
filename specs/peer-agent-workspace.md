# Peer Agent workspace

Status: proposal; not implemented or approved for implementation. This public
summary preserves the design questions without personal deployment inventories.

## Intended use

Allow explicitly paired Astra installations to collaborate in their own local
environments. Either peer can request a task, exchange clarifications and return
an artifact. Reaching a peer outside the local network is a core requirement;
a successful same-network demo would not satisfy it.

Each installation owns its identity, permissions, settings and memory. A task
brief shares only the context needed for that task. This is not automatic memory
merging, whole-history access or bidirectional working-directory synchronization.

## Candidate design

The existing [Worker and Team runtime](../docs/design/worker-runtime-v1.md) provides
useful local task and message concepts. A remote transport may reuse those
concepts, but its storage, locking and failure behavior must be checked for
cross-process operation before adopting them.

- Pair known installations explicitly and exchange a bounded capability summary.
  Authentication, encryption, revocation and replay protection require a reviewed
  protocol; local trust alone is not sufficient.
- Use site-qualified identifiers and monotonic sequence numbers for ordering and
  deduplication. Do not use wall-clock timestamps as message identity.
- Keep a durable outbox and bounded delivery deadlines. A disconnection must leave
  the task's last known status visible, without silently repeating an operation.
- Let the executing peer own task progress and request additional input when
  needed. Cancellation and reconnection need explicit acknowledgments.
- Send artifact metadata separately from file bytes. Validate declared size and
  content hash, land files in an inbox, and avoid overwriting an active workspace.
  Preserve both copies of conflicting binary documents.

## Decisions still required

| Topic | Question to resolve |
| --- | --- |
| Reachability | Which direct, tunnel or relay path works on the actual networks? |
| Availability | Is the peer always running, or what supported mechanism can wake it? |
| Pairing | How are credentials established, revoked and renewed? |
| Task lifecycle | Which states survive restart, and when may a sender safely retry? |
| File access | Which directories are shared, and how are incoming files accepted? |
| Concurrency | Which operations require a lease, and what happens after expiry? |
| Context | What is the brief budget, and how does a peer request missing evidence? |

Network reachability and waking a powered-down machine are separate requirements.
Both need deployment evidence; neither follows from the presence of an HTTP API.

## Acceptance before release

1. From outside the local network, reach a freshly restarted peer and complete a
   read-only task with the configured authentication.
2. Complete a task that asks for clarification, resumes and delivers an artifact
   whose bytes match the declared hash. Repeat with the roles reversed.
3. Disconnect and restart during delivery; demonstrate no lost accepted message
   and no repeated side effect. Distinguish unknown outcome from safe retry.
4. Reject an unpaired peer, revoked credential, replayed request, invalid artifact
   and write outside the declared access scope.
5. Demonstrate cancellation, peer unavailability, conflicting file edits and
   lease expiry without claiming unverified completion.

Tests should use synthetic files and injected clocks. Real-network acceptance
requires a separate explicit deployment; it is not part of this proposal.
