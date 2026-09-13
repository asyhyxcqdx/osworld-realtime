# Realtime GUI Agent Project Guide

This fork extends OSWorld with a real-time GUI benchmark and four controlled
agent variants. The research question is whether turn-based agents become more
capable when they receive historical video frames, can submit multiple actions
in one turn, or receive both capabilities.

## Benchmark

The current benchmark contains 69 self-contained browser games:

| Category | Capability requirement | Tasks |
| --- | --- | ---: |
| A | Continuous perception; no real-time action requirement | 22 |
| B | Anticipatory action; no real-time perception requirement | 12 |
| C | Coupled perception and action with reproducible dynamics | 17 |
| D | Coupled perception and action with stochastic dynamics | 18 |

The task list is `evaluation_examples/test_realtime_gui_bench.json`. Game pages
are in `evaluation_examples/websites/realtime_gui_bench/games/`, and the
corresponding OSWorld task configurations are in
`evaluation_examples/examples/realtime_gui_bench/`.

Each game provides up to three attempts within one environment run. The
evaluator reads `BENCH.passed` and `BENCH.attempts` and reports:

- `pass@1`: the first attempt succeeds (`passed` is true and `attempts == 0`).
- `pass@3`: at least one of the three attempts succeeds (`passed` is true).

`pass@3` therefore does not require running the same game three separate
times. A missing or interrupted result is unscored; it must not be converted to
a game failure.

## Agent matrix

All four variants use the same screenshot observation and the existing
`computer_13` action vocabulary. The experimental factors are:

| Agent | Historical-frame tool | Actions per turn |
| --- | --- | --- |
| Agent1 | Disabled | Exactly one atomic action |
| Agent2 | Disabled | One submitted action sequence |
| Agent3 | Enabled | Exactly one atomic action |
| Agent4 | Enabled | One submitted action sequence |

A turn is one perception--reasoning--action cycle. Agent3 and Agent4 may call
the historical-frame tool repeatedly before committing an action. Tool calls do
not add turns. Agent2 and Agent4 may execute several atomic actions after one
action submission; Agent1 and Agent3 are restricted to one.

The intended final interface is native API tool use with schemas for both
historical-frame queries and action submission. The current code already has
the frame-query protocol and action validation, but action responses are still
being migrated from text JSON parsing to the unified action tool interface.

## Runtime architecture

- `mm_agents/realtime_protocol.py`: variant modes, action vocabulary validation,
  frame-query schema, and prompt construction.
- `mm_agents/realtime_agent.py`: provider protocol adapters, tool-call loop,
  response logging, and frame-query handling.
- `lib_run_realtime.py`: task lifecycle, turn/action budgets, recordings, and
  detailed result logging.
- `desktop_env/server/realtime.py`: VM-side continuous recording, frame access,
  and action-sequence execution.
- `desktop_env/server/fmp4.py`: incremental fMP4 indexing and historical-frame
  decoding.
- `scripts/python/run_multienv.py`: the command-line entry point for the four
  variants.
- `desktop_env/evaluators/getters/realtime_gui.py` and
  `desktop_env/evaluators/metrics/realtime_gui.py`: result extraction and
  `pass@1`/`pass@3` scoring.

## Verification status

The 69 games and their evaluator have been checked with browser interaction
and scoring-path tests. The four-agent plumbing has local and API smoke-test
coverage. These checks establish that the benchmark can run and score; they do
not constitute the final model comparison.

The full experiment is 69 tasks × 4 variants. The old Sonnet smoke batch is a
partial diagnostic run and must not be presented as the final 4×4 result
matrix. New experiments must use a new `run_id` when changing the model,
protocol, prompt, or budget.

For a four-agent comparison, pass one shared `--result_dir` and one shared
`--run_id` to the four launches. The runner creates the isolated layout
`<result_dir>/<model>/<run_id>/agent1` through `agent4`; reusing the same
`run_id` resumes that comparison batch rather than mixing it with another
configuration. The canonical command template is in
`AGENT_EXPERIMENT_DESIGN.md`.

## Contributor workflow

1. Read this guide, `AGENT_EXPERIMENT_DESIGN.md`, and the relevant tests.
2. Run focused unit tests before changing runtime behavior.
3. Keep benchmark pages, task configs, evaluator logic, and agent logic in
   separate changes where possible.
4. Use `.env` only for local credentials; never commit API keys, VM images,
   recordings, caches, or private result logs.
5. Document any change to the action schema, turn definition, timing, scoring,
   or result layout before running a new experiment.

The handoff files record historical decisions and interruptions. They are useful
for archaeology, while this guide is the intended starting point for new
contributors.
