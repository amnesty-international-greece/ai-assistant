# CLAUDE.md

Guidance for AI agents and contributors working in this repository.

## Workflow steps

Each workflow in `src/workflows/` is a `BaseWorkflow` subclass with an ordered
list of `WorkflowStep`s and a `@staticmethod debug_fixture()` returning a fake
`ctx`. The `debug` CLI group runs any single step in isolation against that
fixture (always in `test_mode`); see [`docs/DEBUG_CLI.md`](docs/DEBUG_CLI.md).

- When you add a `WorkflowStep` to any workflow, extend that workflow's
  `debug_fixture()` with every new ctx key the step reads, so
  `python -m src.cli debug run <workflow> <step>` works without a KeyError. If
  the step is pure (no external I/O), add it to the allow-list in
  `tests/workflows/test_debug_fixtures.py`.

## Tests

Run the suite with `python -m pytest -q`. Keep test assertions ASCII-safe
(the Windows console may be cp1253); assert on the type/shape of values rather
than exact Greek text where practical.
