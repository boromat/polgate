---
name: add-guard
description: Add a new stage-1 detector (Guard) or a new entity type to the polanon library, which the hub installs from PyPI, then make the hub require the release that has it. Use when asked to detect a new kind of data (e.g. "add KRS detection", "detect Polish license plates", "add a GLiNER guard").
argument-hint: <what to detect>
---

# Add a guard / entity type: $ARGUMENTS

Detection is not in this repository. The guards live in polanon
([github.com/boromat/polanon](https://github.com/boromat/polanon)), which the hub installs from PyPI as
`polanon[ner,judge]`. The change is made there, released, and only then picked up here. Never add a
guard to `polgate/`, copy polanon code into this repository, or patch the installed package.

## 1. In polanon
Work in a local clone of polanon, by default the sibling folder `../polanon`; clone
`https://github.com/boromat/polanon` there if it is missing. Read its `CONTRIBUTING.md` and the
README section *Adding a detector* first: they are the rules in that repository, which has no
`CLAUDE.md`. polanon is a standalone library: the core imports nothing but the standard library at
module level, heavy dependencies are imported inside the functions that need them, and it never
imports `polgate` or a web or database framework (`tests/test_layering.py` enforces both).

1. **Entity type**: if new, add it to `polanon/policy.py::ENTITY_TYPES` with a Polish token label
   (employees see tokens in previews, as `"PLATE": "REJESTRACJA"` does). Uppercase letters and
   underscores, and not a label another type already uses.
2. **Detector**:
   - Deterministic pattern with a checksum or strict format -> add an `(entity_type, regex, validator)`
     rule to `PATTERNS` in `polanon/guards/recognizers_pl.py` and an `is_valid_*` function. Use
     lookarounds `(?<!\d)` / `(?!\d)` to avoid matching inside longer numbers. Foreign formats belong
     in `polanon/guards/international.py`.
   - Statistical / ML detector -> new file `polanon/guards/<name>.py` with `class XGuard(Guard)`,
     lazy-load the model inside `detect` or a cached property, accept model names via the constructor.
     Register it in `polanon/__init__.py::default_guards` behind a keyword flag if it is heavy.
3. **Spans** are in ORIGINAL text coordinates, `end` exclusive, `source` the guard's name. Respect the
   `enabled` set: return `[]` fast when your entity types are not enabled. Score 1.0 for what a
   checksum, a label or an official format vouches for, 0.85 for a statistical guess.
4. **Tests** in polanon's `tests/test_recognizers_pl.py` or `tests/test_<name>_guard.py`:
   - valid samples detected, invalid checksum rejected, no match inside longer digit runs,
   - `enabled` filtering, overlap with an existing entity resolved by `merge_spans` (higher
     confidence wins, length breaks ties, same-type overlaps are joined).
   - Generate valid sample values in the test with a tiny helper instead of hard-coding from memory.
     Synthetic data only: a PESEL such as 50810112354, addresses at `example.com`.
5. **Measure**: `python benchmarks/run.py --ner` before and after the change. Add a case worth keeping
   to `benchmarks/samples/` and `benchmarks/gold/`; `tests/test_benchmark.py` holds the numbers as
   floors, so a guard that makes detection worse fails the build.
6. **Docs**: the README section *What it detects* if the list changes, and an entry under
   `## [Unreleased]` in polanon's `CHANGELOG.md`.
7. Run polanon's quality bar: `ruff check .`, `ruff format --check .`, `mypy polanon`, `pytest --cov`.
   Commit `feat(guards): ...` in the polanon repository.
8. **Release**: raising `__version__` in `polanon/__init__.py`, dating the changelog section and pushing
   a `v*` tag publishes the version to PyPI and GitHub. That is the owner's decision: stop here, report
   what changed, and ask before pushing or tagging anything.

## 2. In the hub, once the release is on PyPI
1. Raise the floor in `pyproject.toml` to the new version, `"polanon[ner,judge]>=X.Y.Z"`, and reinstall:
   `.venv/bin/pip install -e ".[dev]"`. To try the change before it is released, install the clone
   instead with `.venv/bin/pip install -e "../polanon[ner,judge]"`, and go back to PyPI afterwards with
   `.venv/bin/pip uninstall -y polanon && .venv/bin/pip install -e ".[dev]"`.
2. **A new entity type** needs no list edit here: `DEFAULT_ENABLED_ENTITIES` in `polgate/models.py` is
   derived from `polanon.policy.ENTITY_TYPES`, and `switch_on_new_entity_types` in
   `polgate/policy_loader.py` turns the type on for installations that already have a policy. If it
   should start switched off, add it to `OFF_BY_DEFAULT` in `polgate/models.py` and cover that in
   `tests/test_policy_upgrade.py`.
3. If `docs/ARCHITECTURE.md` or the README lists what is detected, update that line.
4. Run `/quality-gate`, then commit with the reason for the new floor in the message, e.g.
   `build(deps): require polanon X.Y.Z, which detects ...`.
