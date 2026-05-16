# Repository Guidelines

## Project Structure & Module Organization

ChatScene is a Python research codebase built around CARLA, SafeBench, and Scenic. Core runtime code lives in `safebench/`: agent policies and configs are under `safebench/agent/`, and scenario configs/data are under `safebench/scenario/`. Main entry points are in `scripts/` (`run_train.py`, `run_eval.py`, and dynamic variants). Retrieval-based Scenic generation lives in `retrieve/`, including prompts and `database_v1.pkl`. CARLA route/scenario utilities are in `tools/CarlaScenariosBuilder/`. Documentation sources are in `docs/source/`; the vendored Scenic package and tests are in `Scenic/`.

## Build, Test, and Development Commands

Use Python 3.8 in the `chatscene` conda environment. Install project dependencies from the repository root:

```bash
pip install -r requirements.txt
pip install decorator==5.1.1
pip install -e .
cd Scenic && python -m pip install -e .
```

Launch CARLA separately, then run examples from the repo root:

```bash
python scripts/run_train.py --agent_cfg=adv_scenic.yaml --scenario_cfg=train_scenario_scenic.yaml --mode train_scenario --scenario_id 1
python scripts/run_eval.py --agent_cfg=adv_scenic.yaml --scenario_cfg=eval_scenic.yaml --mode eval --scenario_id 1 --test_epoch -1
python retrieve/retrieve.py --topk 3
```

Build documentation with `cd docs && make html`.

## Coding Style & Naming Conventions

Follow the existing Python style: 4-space indentation, snake_case for functions, variables, and modules, and lowercase YAML config names such as `train_agent_scenic.yaml`. Keep runner scripts argument-driven with `argparse`, and place reusable logic in `safebench/`, `retrieve/`, or `tools/`. Prefer explicit paths built from `ROOT_DIR` or `os.path` helpers.

## Testing Guidelines

There is no dedicated top-level ChatScene test suite. For environment smoke checks, run `python tools/test_env.py` after CARLA and the Python API are configured. For Scenic changes, run targeted tests such as `cd Scenic && pytest tests/core/test_geometry.py`. For scenario or agent changes, validate with the smallest relevant train/eval command and record the scenario ID, config files, seed, CARLA port, and result location.

## Commit & Pull Request Guidelines

This checkout does not include Git history, so use concise imperative commit messages, for example `fix dynamic scenic output path`. Pull requests should explain the affected mode (`train_scenario`, `train_agent`, `eval`, or dynamic), list changed configs/data paths, include reproduction commands, and attach screenshots or logs when CARLA behavior, rendered scenes, or metrics change. Do not commit local CARLA installs, generated logs, credentials, or private OpenAI keys.

## Security & Configuration Tips

Keep `CARLA_ROOT`, `PYTHONPATH`, display settings, and API keys in your shell environment, not in tracked files. Check large generated artifacts before committing, especially under `log/`, model checkpoints, and scenario data directories.
