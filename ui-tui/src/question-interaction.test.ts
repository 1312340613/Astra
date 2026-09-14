import assert from "node:assert/strict";
import test from "node:test";

import {
  createQuestionState,
  formatQuestionAnswerSummary,
  transitionQuestion,
} from "./question-interaction.js";
import type { UserQuestionRequest } from "./types.js";

const request: UserQuestionRequest = {
  type: "user_question_request",
  request_id: "question-1",
  questions: [
    {
      id: "storage",
      header: "Storage",
      question: "Choose storage",
      options: [
        { label: "SQLite (Recommended)", description: "Session local" },
        { label: "Markdown", description: "Human editable" },
      ],
      multi_select: false,
    },
    { id: "notes", question: "Any constraints?", multi_select: false },
  ],
};

function multiRequest(): UserQuestionRequest {
  return {
    type: "user_question_request",
    request_id: "question-multi",
    questions: [
      {
        id: "features",
        question: "Choose features",
        options: [
          { label: "Search" },
          { label: "Export" },
          { label: "Sync" },
        ],
        multi_select: true,
      },
    ],
  };
}

function multiBatchRequest(): UserQuestionRequest {
  const multi = multiRequest();
  return {
    ...multi,
    request_id: "question-multi-batch",
    questions: [
      multi.questions[0]!,
      { id: "notes", question: "Any constraints?", multi_select: false },
    ],
  };
}

function action(
  state: ReturnType<typeof createQuestionState>,
  input: Parameters<typeof transitionQuestion>[1],
) {
  return transitionQuestion(state, input);
}

test("single selection and custom answer submit one ordered batch", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "select", label: "SQLite (Recommended)" }).state;
  state = action(state, { type: "next" }).state;
  state = action(state, { type: "commit_custom", text: "Keep Windows behavior" }).state;
  const submitted = action(state, { type: "submit" });
  assert.deepEqual(submitted.response?.answers, [
    { id: "storage", selected: ["SQLite (Recommended)"] },
    { id: "notes", selected: [], custom: "Keep Windows behavior" },
  ]);
  assert.deepEqual(submitted.response?.requestId, "question-1");
});

test("skip preserves other drafts and advances to the next question", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "skip" }).state;
  assert.equal(state.index, 1);
  state = action(state, { type: "commit_custom", text: "No extra constraints" }).state;
  const submitted = action(state, { type: "submit" });
  assert.deepEqual(submitted.response?.answers[0], { id: "storage", selected: [] });
  assert.deepEqual(submitted.response?.answers[1], {
    id: "notes",
    selected: [],
    custom: "No extra constraints",
  });
});

test("multi-select toggles labels without mutating the request", () => {
  const multi = multiRequest();
  const original = structuredClone(multi);
  let state = createQuestionState(multi);
  state = action(state, { type: "toggle", label: "Search" }).state;
  state = action(state, { type: "toggle", label: "Export" }).state;
  assert.deepEqual(state.drafts[0].selected, ["Search", "Export"]);
  state = action(state, { type: "toggle", label: "Search" }).state;
  assert.deepEqual(state.drafts[0].selected, ["Export"]);
  assert.deepEqual(multi, original);
});

test("guarded next keeps an unanswered multi-select question current", () => {
  const state = createQuestionState(multiBatchRequest());
  const blocked = action(state, { type: "next_if_complete" });

  assert.equal(blocked.state.index, 0);
  assert.match(blocked.state.error, /Select at least one option, enter a custom answer, or press S to skip/);
  assert.deepEqual(blocked.state.drafts[0].selected, []);
});

test("guarded next advances after a multi-select option is selected", () => {
  let state = createQuestionState(multiBatchRequest());
  state = action(state, { type: "toggle", label: "Search" }).state;
  const advanced = action(state, { type: "next_if_complete" });

  assert.equal(advanced.state.index, 1);
  assert.equal(advanced.state.error, "");
  assert.deepEqual(advanced.state.drafts[0].selected, ["Search"]);
});

test("single-select replacement leaves only the most recently selected option", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "select", label: "SQLite (Recommended)" }).state;
  state = action(state, { type: "select", label: "Markdown" }).state;
  assert.deepEqual(state.drafts[0].selected, ["Markdown"]);
  assert.deepEqual(state.drafts[0].custom, undefined);
});

test("previous and next stay within question bounds", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "previous" }).state;
  assert.equal(state.index, 0);
  state = action(state, { type: "next" }).state;
  assert.equal(state.index, 1);
  state = action(state, { type: "next" }).state;
  assert.equal(state.index, 1);
  state = action(state, { type: "previous" }).state;
  assert.equal(state.index, 0);
});

test("custom answers override a single-select choice and trim surrounding whitespace", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "select", label: "Markdown" }).state;
  state = action(state, { type: "commit_custom", text: "  Use JSON  " }).state;
  assert.deepEqual(state.drafts[0], {
    id: "storage",
    selected: [],
    custom: "Use JSON",
    skipped: false,
  });
});

test("blank custom answers are rejected without changing the draft", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "select", label: "Markdown" }).state;
  const rejected = action(state, { type: "commit_custom", text: " \t " });
  assert.equal(rejected.response, undefined);
  assert.match(rejected.state.error, /custom/i);
  assert.deepEqual(rejected.state.drafts[0].selected, ["Markdown"]);
  assert.equal(rejected.state.drafts[0].custom, undefined);
});

test("retryable submission rejection preserves the draft and surfaces the reason", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "toggle", label: "SQLite (Recommended)" }).state;

  const rejected = action(state, {
    type: "submission_rejected",
    reason: "Selected label was not offered.",
  });

  const comparableDrafts = (drafts: typeof state.drafts) => drafts.map((draft) => ({
    id: draft.id,
    selected: draft.selected,
    custom: draft.custom,
    skipped: draft.skipped,
  }));
  assert.deepEqual(comparableDrafts(rejected.state.drafts), comparableDrafts(state.drafts));
  assert.equal(rejected.state.index, state.index);
  assert.equal(rejected.state.error, "Selected label was not offered.");
  assert.equal(rejected.response, undefined);
});

test("custom answers over the backend's 2,000-character limit are retryable", () => {
  let state = createQuestionState(request);
  state = action(state, { type: "select", label: "Markdown" }).state;
  const rejected = action(state, { type: "commit_custom", text: "x".repeat(2_001) });

  assert.equal(rejected.response, undefined);
  assert.match(rejected.state.error, /2,000/);
  assert.deepEqual(rejected.state.drafts[0].selected, ["Markdown"]);
  assert.equal(rejected.state.drafts[0].custom, undefined);
});

test("custom answer cleaning matches the backend at the exact limit", () => {
  const custom = "x".repeat(1_998) + "\x00y";
  const accepted = action(createQuestionState(request), { type: "commit_custom", text: custom });

  assert.equal(accepted.state.error, "");
  assert.equal(accepted.state.drafts[0].custom, "x".repeat(1_998) + " y");
});

test("submit rejects incomplete drafts and clears the error after a valid action", () => {
  const incomplete = action(createQuestionState(request), { type: "submit" });
  assert.equal(incomplete.response, undefined);
  assert.match(incomplete.state.error, /complete|answer|skip/i);

  let state = incomplete.state;
  state = action(state, { type: "select", label: "Markdown" }).state;
  state = action(state, { type: "next" }).state;
  state = action(state, { type: "skip" }).state;
  const submitted = action(state, { type: "submit" });
  assert.ok(submitted.response);
  assert.equal(submitted.state.error, "");
});

test("cancel returns only the matching request id and does not mutate state", () => {
  const state = createQuestionState(request);
  const cancelled = action(state, { type: "cancel" });
  assert.deepEqual(cancelled.cancellation, { requestId: "question-1" });
  assert.equal(cancelled.response, undefined);
  assert.deepEqual(cancelled.state, state);
});

test("each request gets independent fresh drafts", () => {
  const first = createQuestionState(request);
  const changed = action(first, { type: "select", label: "Markdown" }).state;
  const second = createQuestionState(request);
  assert.deepEqual(first.drafts[0].selected, []);
  assert.deepEqual(changed.drafts[0].selected, ["Markdown"]);
  assert.deepEqual(second.drafts[0].selected, []);
  assert.notEqual(first.drafts, second.drafts);
});

test("summary is compact and user facing", () => {
  assert.equal(
    formatQuestionAnswerSummary(request, [
      { id: "storage", selected: ["SQLite (Recommended)"] },
      { id: "notes", selected: [], custom: "Keep Windows behavior" },
    ]),
    "Choice · Storage: SQLite (Recommended) · notes: Keep Windows behavior",
  );
});
