#!/bin/bash
# x40_tx_up.sh: bring the X40 dpmg_svc up with the RT budget, atomically.
# Docker zeros /docker/cpu.rt_runtime_us whenever a non-RT container starts, so
# the grant and the RT container launch must happen with nothing in between.
# Run on the X40 (192.168.0.26):  bash deploy/radio/x40_tx_up.sh   (prompts once for sudo)
# APP is the code folder mounted at /app_svc.
set -e
APP="${APP:-/home/sidekiq/work/NiceGui_test}"

docker rm -f dpmg_svc 2>/dev/null || true

# grant RT: parent first, then the docker child (nothing is running to reset it)
echo 950000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/cpu.rt_runtime_us >/dev/null
sudo mkdir -p /sys/fs/cgroup/cpu,cpuacct/docker
echo 900000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us >/dev/null

docker run -d --name dpmg_svc --restart unless-stopped --network host \
  --privileged --cpu-rt-runtime=900000 --ipc=host -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache \
  -v "$APP":/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro \
  dpmg:latest \
  python dpmg_service.py --kind x40 --host-type x40 --serial X40-0001 \
    --cmd-port 47810 --discovery-port 47811 --broadcast 192.168.0.25

sleep 3
echo "--- result ---"
echo -n "docker rt_runtime: "; cat /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us
docker inspect dpmg_svc --format 'RT={{.HostConfig.CpuRealtimeRuntime}} status={{.State.Status}}'
