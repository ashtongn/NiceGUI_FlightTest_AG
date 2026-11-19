#!/usr/bin/env python3
"""Write the docker run line that makes a container like the one in a saved
`docker inspect` (the payload's dpmg_svc before the site deploy), on a given image.

    python3 old_run_cmd.py INSPECT.json IMAGE > old_run_cmd.sh

Kept: name, restart policy, host network, privileged, IPC mode, working dir, the binds,
the DPMG_* and PYTHON* environment, the command. Used by site_rollback.sh container.
"""
import json
import shlex
import sys


def run_line(c, image):
    hc, cfg = c["HostConfig"], c["Config"]
    a = ["docker", "run", "-d", "--name", c["Name"].lstrip("/")]
    rp = (hc.get("RestartPolicy") or {}).get("Name")
    if rp and rp != "no":
        a += ["--restart", rp]
    if hc.get("NetworkMode") == "host":
        a += ["--network", "host"]
    if hc.get("Privileged"):
        a.append("--privileged")
    if hc.get("IpcMode") in ("host",):
        a.append("--ipc=host")
    if hc.get("Runtime") and hc["Runtime"] != "runc":
        a += ["--runtime", hc["Runtime"]]
    if cfg.get("WorkingDir"):
        a += ["-w", cfg["WorkingDir"]]
    for e in cfg.get("Env") or []:
        if e.startswith(("DPMG_", "PYTHON")) and not e.startswith("PYTHON_VERSION") \
                and not e.startswith("PYTHON_SHA256") and not e.startswith("PYTHON_GET_PIP"):
            a += ["-e", e]
    for b in hc.get("Binds") or []:
        a += ["-v", b]
    a.append(image)
    a += cfg.get("Cmd") or []
    return " ".join(shlex.quote(x) for x in a)


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    c = data[0] if isinstance(data, list) else data
    print("#!/usr/bin/env bash")
    print("# made by old_run_cmd.py from the saved docker inspect; run on the G40")
    print(run_line(c, sys.argv[2]))
