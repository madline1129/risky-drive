/mnt/data2/congfeng/miniconda3/envs/resim/bin/python \
    /mnt/data2/whz/risky-drive/carla_smoke/scenes/safebench_scenic_scene.py \
    --carla-root /mnt/data2/congfeng/CARLA \
    --host 127.0.0.1 \
    --port 2001 \
    --timeout 300.0 \
    --scenic-file ./risky-weaver/opencode/workdir/generated_scene.scenic \
    --scene-sample-attempts 20 \
    --frames 180 \
    --save-every 5 \
    --warmup-ticks 5 \
    --seed 7 \
    --timestep 0.05 \
    --ego-speed-difference -5.0 \
    --weather ClearNoon \
    --camera-mode surround \
    --output-dir ./risky-weaver/opencode/workdir/images \
    --clean-output
