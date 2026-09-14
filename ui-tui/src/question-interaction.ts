import type {
  UserQuestion,
  UserQuestionAnswer,
  UserQuestionRequest,
} from "./types.js";

export const MAX_CUSTOM_ANSWER_CHARS = 2_000;

export interface QuestionDraft extends UserQuestionAnswer {
  skipped: boolean;
}

export interface QuestionInteractionState {
  request: UserQuestionRequest;
  index: number;
  drafts: QuestionDraft[];
  error: string;
}

export type QuestionAction =
  | { type: "select"; label: string }
  | { type: "toggle"; label: string }
  | { type: "commit_custom"; text: string }
  | { type: "skip" }
  | { type: "previous" }
  | { type: "next" }
  | { type: "next_if_complete" }
  | { type: "submit" }
  | { type: "submission_rejected"; reason: string }
  | { type: "cancel" };

export interface QuestionTransition {
  state: QuestionInteractionState;
  response?: {
    requestId: string;
    answers: UserQuestionAnswer[];
  };
  cancellation?: {
    requestId: string;
  };
}

function copyDraft(draft: QuestionDraft): QuestionDraft {
  return {
    id: draft.id,
    selected: [...draft.selected],
    ...(draft.custom === undefined ? {} : { custom: draft.custom }),
    skipped: draft.skipped,
  };
}

function copyDrafts(drafts: QuestionDraft[]): QuestionDraft[] {
  return drafts.map(copyDraft);
}

function withState(
  state: QuestionInteractionState,
  changes: Partial<Pick<QuestionInteractionState, "index" | "drafts" | "error">>,
): QuestionInteractionState {
  return {
    request: state.request,
    index: changes.index ?? state.index,
    drafts: changes.drafts ?? copyDrafts(state.drafts),
    error: changes.error ?? "",
  };
}

function currentQuestion(state: QuestionInteractionState): UserQuestion | undefined {
  return state.request.questions[state.index];
}

function currentDraft(state: QuestionInteractionState): QuestionDraft | undefined {
  return state.drafts[state.index];
}

function isOffered(question: UserQuestion, label: string): boolean {
  return question.options?.some((option) => option.label === label) ?? false;
}

function invalidOption(state: QuestionInteractionState): QuestionTransition {
  return {
    state: {
      request: state.request,
      index: state.index,
      drafts: copyDrafts(state.drafts),
      error: "That option is not available for this question.",
    },
  };
}

function updateCurrentDraft(
  state: QuestionInteractionState,
  update: (draft: QuestionDraft, question: UserQuestion) => QuestionDraft,
): QuestionTransition {
  const question = currentQuestion(state);
  const draft = currentDraft(state);
  if (!question || !draft) return { state: withState(state, {}) };

  const drafts = copyDrafts(state.drafts);
  drafts[state.index] = update(copyDraft(draft), question);
  return { state: withState(state, { drafts }) };
}

function answerIsComplete(draft: QuestionDraft): boolean {
  return draft.skipped || draft.selected.length > 0 || Boolean(draft.custom);
}

function advanceToNextQuestion(state: QuestionInteractionState): QuestionInteractionState {
  return withState(state, {
    index: Math.min(state.index + 1, Math.max(0, state.request.questions.length - 1)),
  });
}

function answerFromDraft(draft: QuestionDraft): UserQuestionAnswer {
  return {
    id: draft.id,
    selected: [...draft.selected],
    ...(draft.custom === undefined ? {} : { custom: draft.custom }),
  };
}

function cleanCustomAnswer(value: string): string {
  let safe = "";
  for (const character of value) {
    const codePoint = character.codePointAt(0) ?? 0;
    safe += character === "\t" || codePoint >= 32 ? character : " ";
  }
  return safe.replace(/\p{White_Space}+/gu, " ").trim();
}

export function createQuestionState(request: UserQuestionRequest): QuestionInteractionState {
  return {
    request,
    index: 0,
    drafts: request.questions.map((question) => ({
      id: question.id,
      selected: [],
      skipped: false,
    })),
    error: "",
  };
}

export function transitionQuestion(
  state: QuestionInteractionState,
  action: QuestionAction,
): QuestionTransition {
  const question = currentQuestion(state);
  const draft = currentDraft(state);

  switch (action.type) {
    case "select": {
      if (!question || !draft || !isOffered(question, action.label)) return invalidOption(state);
      return updateCurrentDraft(state, (current) => ({
        ...current,
        selected: [action.label],
        custom: question.multi_select ? current.custom : undefined,
        skipped: false,
      }));
    }

    case "toggle": {
      if (!question || !draft || !isOffered(question, action.label)) return invalidOption(state);
      return updateCurrentDraft(state, (current) => {
        if (!question.multi_select) {
          return { ...current, selected: [action.label], custom: undefined, skipped: false };
        }
        const selected = current.selected.includes(action.label)
          ? current.selected.filter((label) => label !== action.label)
          : [...current.selected, action.label];
        return { ...current, selected, skipped: false };
      });
    }

    case "commit_custom": {
      const text = cleanCustomAnswer(action.text);
      if (!text) {
        return {
          state: {
            request: state.request,
            index: state.index,
            drafts: copyDrafts(state.drafts),
            error: "Custom answer cannot be blank.",
          },
        };
      }
      if (Array.from(text).length > MAX_CUSTOM_ANSWER_CHARS) {
        return {
          state: {
            request: state.request,
            index: state.index,
            drafts: copyDrafts(state.drafts),
            error: `Custom answer must be at most ${MAX_CUSTOM_ANSWER_CHARS.toLocaleString("en-US")} characters.`,
          },
        };
      }
      return updateCurrentDraft(state, (current) => ({
        ...current,
        selected: question?.multi_select ? current.selected : [],
        custom: text,
        skipped: false,
      }));
    }

    case "skip": {
      if (!draft) return { state: withState(state, {}) };
      const drafts = copyDrafts(state.drafts);
      drafts[state.index] = { id: draft.id, selected: [], skipped: true };
      return {
        state: withState(state, {
          drafts,
          index: Math.min(state.index + 1, Math.max(0, state.request.questions.length - 1)),
        }),
      };
    }

    case "previous":
      return {
        state: withState(state, { index: Math.max(0, state.index - 1) }),
      };

    case "next_if_complete":
      if (question?.multi_select && draft && !answerIsComplete(draft)) {
        return {
          state: withState(state, {
            error: "Select at least one option, enter a custom answer, or press S to skip.",
          }),
        };
      }
      return { state: advanceToNextQuestion(state) };

    case "next":
      return {
        state: advanceToNextQuestion(state),
      };

    case "submit": {
      if (state.drafts.some((item) => !answerIsComplete(item))) {
        return {
          state: {
            request: state.request,
            index: state.index,
            drafts: copyDrafts(state.drafts),
            error: "Answer every question or skip it before submitting.",
          },
        };
      }
      const answers = state.drafts.map(answerFromDraft);
      return {
        state: withState(state, {}),
        response: { requestId: state.request.request_id, answers },
      };
    }

    case "submission_rejected":
      return {
        state: withState(state, { error: action.reason }),
      };

    case "cancel":
      return {
        state: {
          request: state.request,
          index: state.index,
          drafts: copyDrafts(state.drafts),
          error: state.error,
        },
        cancellation: { requestId: state.request.request_id },
      };
  }
}

export function formatQuestionAnswerSummary(
  request: UserQuestionRequest,
  answers: UserQuestionAnswer[],
): string {
  const byId = new Map(answers.map((answer) => [answer.id, answer]));
  const parts = request.questions.map((question) => {
    const answer = byId.get(question.id);
    const values = answer
      ? [...answer.selected, ...(answer.custom ? [answer.custom] : [])]
      : [];
    return `${question.header ?? question.id}: ${values.length > 0 ? values.join(", ") : "Skipped"}`;
  });
  return parts.length > 0 ? `Choice · ${parts.join(" · ")}` : "Choice";
}
