python /mnt/data2/whz/risky-drive/carla_smoke/pipeline/l4.py \
/mnt/data2/whz/risky-drive/carla_smoke/workdir/20260516_234902/l3/chains.json \
    --chain-index 0 \
    --output-dir /mnt/data2/whz/risky-drive/carla_smoke/workdir/20260516_234902/l4 \
    --l0-json /mnt/data2/whz/risky-drive/carla_smoke/workdir/20260516_234902/l0/state.json \
    --carla-root /mnt/data2/congfeng/carla915 \
    --host 127.0.0.1 \
    --port 2000 \
    --town Town03 \
    --frames 140 \
    --save-every 5 \
    --code-agent opencode \
    --opencode-bin opencode \
    --opencode-model ds-v4-fast \
    --execute
