# Repository Guidelines

## Project Structure & Module Organization

GameTune Agent is a Python/PySide6 desktop application for game-performance optimization.

- `app.py`: desktop entry point, pages, signals, and styling.
- `agent/`: controller, workflow state machine, planning, validation, evaluation, and optional model explanations.
- `models/schemas.py`: typed dataclasses and domain validation.
- `tools/`: CSV import, metric calculations, evidence analysis, resolution utilities, and hardware catalog access.
- `knowledge/`: JSON game rules and hardware catalog data.
- `storage/repository.py`: SQLite persistence; local databases reside in `.data/`.
- `tests/test_core.py`: core regression tests. `.qa/` contains generated screenshots. Both output directories are ignored.

## Build, Test, and Development Commands

Run commands from the project root:

```powershell
python -m pip install -r requirements.txt
python app.py
python -m unittest discover -s tests -v
python app.py --screenshot .qa/dashboard.png --page 0
```

These install dependencies, launch the app, run tests, and capture a page. Screenshot indices range from 0 to 4. No build command is configured.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow existing dataclass and enum patterns. Keep UI text in Chinese and files in UTF-8. No formatter or linter is configured; avoid unrelated reformatting.

## Architecture & Data Integrity

Keep metric calculations and decisions deterministic; model explanations must not change measured values or actions. Preserve offline fallback. Validate plans against game rules and route workflow changes through the state machine. Derive diagnoses from telemetry and controlled experiments, not hardware names. Bind UI results to domain records rather than demonstration constants. Distinguish proposed settings from confirmed execution, and preserve configuration provenance and test comparability.

## Testing Guidelines

Use standard-library `unittest`, `test_*.py` files, and `test_*` methods. Add meaningful regression cases for changed metrics, import validation, planning, state transitions, and persistence. Use temporary databases and fake model providers. No numerical coverage target is configured. Run the full suite after core changes; inspect affected screenshots for UI changes and report interaction checks separately.

## Commit & Pull Request Guidelines

This checkout has no Git metadata; historical conventions cannot be verified. Use imperative subjects, such as `Fix experiment round advancement`. Keep changes focused. PRs should explain behavior, reference relevant issues, list validation and limitations, and include screenshots for UI changes.

## Security & Configuration

Keep model credentials in `GAMETUNE_API_BASE`, `GAMETUNE_API_KEY`, `GAMETUNE_MODEL`, and optional `GAMETUNE_API_TIMEOUT` environment variables. Never commit credentials, local databases, or captured user data. Do not log authorization headers.
