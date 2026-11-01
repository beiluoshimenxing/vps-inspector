"""Render an illustrative UI fixture, never a real benchmark. Usage: python tools/preview.py [output.png]."""
from pathlib import Path
import json
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inspector as app


def fixture_run(cmd, timeout=15):
    if cmd[0] == "lscpu":
        return 0, "Architecture: x86_64\nCPU(s): 1\nModel name: AMD EPYC 7V13 64-Core Processor\nThread(s) per core: 1\nCore(s) per socket: 1\nHypervisor vendor: KVM\nVirtualization type: full"
    if cmd[0] == "lsblk":
        return 0, json.dumps({"blockdevices": [{"name": "sda", "type": "disk", "size": 10 * 1024**3, "rota": False, "model": "QEMU HARDDISK"}]})
    if cmd[0] == "df":
        return 0, "Filesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/sda3 ext4 10415295692 3228741664 6764573491 33% /"
    if cmd[0] == "ip":
        return 0, "lo UNKNOWN 127.0.0.1/8 ::1/128\neth0 UP 192.0.2.10/24 2001:db8::10/64"
    if cmd[0] == "ping":
        return 0, "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 0.811/0.869/0.947/0.055 ms"
    if cmd[0] == "curl":
        return 0, "HTTP=200 DNS=0.008865s TCP=0.009749s TLS=0.023637s TTFB=0.029928s total=0.131032s"
    if cmd[0] == "fio":
        if "--version" in cmd:
            return 0, "fio-3.36"
        rw = next((x.split("=", 1)[1] for x in cmd if x.startswith("--rw=")), "write")
        bandwidth = {"write": 1823.41, "read": 2029.06, "randwrite": 49.44, "randread": 28.65}[rw]
        kind = "read" if "read" in rw else "write"
        return 0, json.dumps({"jobs": [{"error": 0, kind: {"bw_bytes": bandwidth * 1048576,
            "iops": bandwidth * (256 if rw.startswith("rand") else 1), "clat_ns": {"mean": 78000}}}]})
    return 0, ""


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "preview.png"
    actual_which = app.shutil.which
    def which(name):
        return None if name == "stress-ng" else name if name in ("lscpu", "lsblk", "df", "ip", "ping", "curl", "fio") else actual_which(name)
    actual_read = app.read
    actual_metric = app.metric
    def metric(label, value, *args, **kwargs):
        return actual_metric(label, str(value).replace(str(Path.cwd()), "/root"), *args, **kwargs)
    def read(path, default="unavailable"):
        return "0.00 0.04 0.06" if str(path) == "/proc/loadavg" else actual_read(path, default)
    with patch.object(app, "run", side_effect=fixture_run), patch.object(app, "cpu_count", return_value=1), \
         patch.object(app, "metric", side_effect=metric), \
         patch.object(app, "read", side_effect=read), patch.object(app.shutil, "which", side_effect=which), \
         patch.object(app.platform, "system", return_value="Linux"), \
         patch.object(app.platform, "freedesktop_os_release", return_value={"PRETTY_NAME": "DEMO DATA / Debian GNU/Linux 13"}), \
         patch.object(app.platform, "release", return_value="6.12.113-demo"), \
         patch.object(app, "available_memory", return_value=553 * 1048576), \
         patch.object(app.shutil, "disk_usage", return_value=type("Usage", (), {"free": int(6.3 * 1024**3)})()), \
         patch.object(app, "meminfo", return_value={"MemTotal": int(967.3 * 1048576), "MemAvailable": 553 * 1048576}):
        app.main(["--modules", "cpu,memory,disk,network", "--stress", "cpu", "--disk-bench", "--duration", "1", "--target", "1.1.1.1", "--png", target])


if __name__ == "__main__":
    main()
