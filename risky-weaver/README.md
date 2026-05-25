# Risky Weaver

Minimal isolated action-primitive -> Scenic generation path using OpenCode.

Run from repository root:

```bash
python risky-weaver/opencode_workdir/primitive_to_scenic.py --env-file .env
```

Useful dry run:

```bash
python risky-weaver/opencode_workdir/primitive_to_scenic.py --dry-run
```

Default files:

- Input primitive: `risky-weaver/opencode_workdir/action_primitive.json`
- OpenCode task: `risky-weaver/opencode_workdir/opencode_task.json`
- Prompt: `risky-weaver/opencode_workdir/opencode_prompt.txt`
- Output Scenic: `risky-weaver/opencode_workdir/generated_scene.scenic`

Model override:

```bash
python risky-weaver/opencode_workdir/primitive_to_scenic.py --model deepseek/deepseek-v4-pro
```
