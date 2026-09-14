# Astra documentation

Start with [English](../README.md) or [简体中文](../README.zh-CN.md) for a short
introduction and quick start. The detailed guides below are primarily in English.

## Installation and daily use

| Task | Guide |
| --- | --- |
| Install, update, keep local changes or recover an interrupted update | [Launcher and updates](launcher-update.md) |
| Select a model, adjust reasoning and use terminal commands | [Everyday use](usage.md) |
| Configure sandboxing, host file access and tool permissions | [Tool execution](execution.md) |
| Understand what is saved, injected or retrieved | [Memory overview](memory.md) |
| Save learned skills, review a batch, migrate old candidates or undo changes | [Skill learning](skill-learning.md) |
| Search earlier conversations and saved observations | [Local history retrieval](local-history-retrieval.md) |
| Look up recorded computer activity | [Activity history](activity-history.md) |
| Configure activity recording | [macOS browser URLs](macos-browser-activity.md), [Windows activity](windows-activity.md) |
| Configure local embeddings | [Context Index platforms](context-index-platforms.md) |
| Operate browser pages and forms | [Browser interaction](browser-interaction.md) |
| Operate a selected Mac application window | [macOS Computer Use](macos-computer-use.md) |
| Attach a window screenshot to a conversation | [Appshot setup and use](appshot.md) |
| Add MCP, web search, QQ messaging, ComfyUI or 163 mail | [Optional integrations](integrations.md) |
| Find and visually inspect image references | [Image search](search-images.md) |
| Run notebooks | [Notebook execution](notebook-execution.md) |
| Diagnose slow responses and tools | [Runtime profiling](runtime-responsiveness.md) |

## How Astra works

| Area | Reference |
| --- | --- |
| Memory selection, retrieval budgets and quality replay | [Native memory](native-memory-recommendation.md) |
| Public Lyra, persona selection and session compatibility | [Persona](persona.md) |
| Core Skill loading and prompt boundaries | [Core rules](runtime/core-rules.md) |
| Tool order, turn deadlines and fault replay | [Turn reliability](runtime/turn-reliability.md) |
| Provider streams, prompt reuse and client recovery | [Provider reliability](provider-reliability.md) |
| Shared capture logic and platform adapters | [Appshot architecture](appshot-architecture.md) |
| Delegation, Team budgets and lifecycle | [Worker runtime](design/worker-runtime-v1.md) |
| Isolated minimal sessions | [Minimal mode](design/minimal-mode.md) |
| Approval requests inside programmatic tool calls | [PTC approval bridge](design/ptc-approval-bridge.md) |

## Testing and compatibility

- [Development and maintenance](development.md) covers locked dependencies,
  local checks, release gates, replay evaluations and artifact cleanup.
- [Computer Use evidence rules](computer-use-evidence.md) distinguish configured
  capabilities, fixture results and live application acceptance.
- [Mac real-machine runbook](macos-computer-use-real-machine-test-runbook.md)
  describes repeatable checks; [Smart Snapshot measurement](macos-smart-snapshot-runbook.md)
  covers the narrower WPS observation experiment.
- The [Windows helper guide](../native/windows-computer-helper/README.md)
  covers its build, tests and platform limits.
- The [transactional app-state matrix](macos-app-state-acceptance.md) and
  [compatibility evidence](macos-computer-compatibility-evidence.md) retain exact
  dated observations used by regression checks. Read their dates and scope:
  they are not a current compatibility guarantee for every app or build.

## Maintaining documentation

Keep current commands, behavior, limitations, architectural decisions and
reproducible test procedures in this directory. Update linked code, Skills and
tests together when moving a document. Historical test counts are not current
release results.

Put implementation plans, task transcripts, handoffs and single-run reports in
ignored local storage (`output/`, `.astra/artifacts/` or `docs/superpowers/`).
Do not force-add ignored planning or validation directories. Preserve useful
conclusions in a current guide before removing a historical report from the
checkout; older tracked versions remain available in Git history.
