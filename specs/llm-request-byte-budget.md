# Outbound request byte budget

Status: investigation proposal, not an implemented byte-budget policy.

## Problem

A model request can contain a large tool observation or image payload even when
its estimated token count is within the context budget. Timeouts after such
observations justify measuring the actual request, but do not establish a
provider's byte limit or identify the cause of a timeout by themselves.

## Existing behavior

`AgentContext` accepts `CONTEXT_MAX_PROMPT_TOKENS` as an override of the default
token budget. Individual tool results and screenshots also have their own bounds.
These limits are not a measurement or cap on the complete serialized HTTP body,
which includes messages, tool schemas, image encodings and JSON overhead.

## Evidence needed

Capture request byte count, estimated tokens, image bytes, schema bytes and
stage timings using synthetic content. Keep model, endpoint, sampling and
transport configuration fixed while varying one payload dimension at a time.
Record explicit provider errors separately from connection or stream timeouts.
Do not log credentials, personal prompts or real tool observations in the report.

## Candidate policy and acceptance

If measurements demonstrate a useful request-size constraint, design a separate
configurable byte budget at the serialization boundary. Decide which old
observations can be compacted and how to report a request that cannot fit.
Preserve the system contract, current user request and tool-call/result pairing;
never silently drop authoritative content to make a payload fit.

Regression cases should include multibyte text, multiple images, large schemas,
near-boundary requests and a minimum request that exceeds the budget. A token
budget test alone must not be described as byte-budget acceptance.
