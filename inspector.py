#!/usr/bin/env python3
"""VPS Inspector: local reports, explicit resource-intensive tests. Python 3.8+."""
import argparse
import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse

VERSION = "1.0.0"
MODULES = ("cpu", "memory", "disk", "network")
LINES = []
ENV = dict(os.environ, LC_ALL="C", LANG="C")


def say(value=""):
    # Escape terminal controls from hostnames, command output and remote responses.
    value = str(value)
    value = "".join(c if c == "\n" or (c.isprintable() and c != "\x7f") else "?" for c in value)
    for line in value.split("\n"):
        LINES.append(line)
        print(line, flush=True)


def section(title):
    say("\n" + "=" * 72)
    say(title)
    say("-" * 72)


def read(path, default="unavailable"):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def stop_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # The group leader may exit before a descendant that ignores SIGTERM.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run(cmd, timeout=15):
    """Every external operation has a deadline and an interruptible process group."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", env=ENV, start_new_session=True)
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        stop_process(proc)
        out, _ = proc.communicate()
        return 124, (out.strip() + "\nTIMEOUT").strip()
    except BaseException:
        stop_process(proc)
        raise


def show_command(cmd, timeout=15):
    code, out = run(cmd, timeout)
    say(out or "No data returned")
    if code:
        say("[UNAVAILABLE/FAILED] exit={}".format(code))
    return code


def meminfo():
    data = {}
    for line in read("/proc/meminfo", "").splitlines():
        key, value = line.split(":", 1)
        data[key] = int(value.split()[0]) * 1024
    return data


def cgroup_value(name):
    # Resolve both cgroup namespaces and nested systemd/docker cgroups.
    candidates = [Path("/sys/fs/cgroup") / name]
    for line in read("/proc/self/cgroup", "").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            relative = parts[2].lstrip("/")
            if ".." not in Path(relative).parts:
                base = Path("/sys/fs/cgroup") / relative
                while Path("/sys/fs/cgroup") in base.parents:
                    candidates.append(base / name)
                    base = base.parent
    values = [read(p, "") for p in candidates]
    return [v for v in values if v and v != "max"]


def available_memory():
    available = meminfo().get("MemAvailable", 0)
    # v2 limits plus v1 fallback. Treat huge v1 sentinel values as unlimited.
    limits = cgroup_value("memory.max")
    currents = cgroup_value("memory.current")
    if limits:
        # Conservative across ancestors; using largest current avoids overestimating.
        available = min(available, max(0, min(map(int, limits)) - max(map(int, currents or ["0"]))))
    limit = read("/sys/fs/cgroup/memory/memory.limit_in_bytes", "")
    used = read("/sys/fs/cgroup/memory/memory.usage_in_bytes", "0")
    if limit.isdigit():
        available = min(available, max(0, int(limit) - int(used)))
    return available


def cpu_count():
    try:
        count = len(os.sched_getaffinity(0))
    except AttributeError:
        count = os.cpu_count() or 1
    for value in cgroup_value("cpu.max"):
        quota, period = value.split()
        if quota != "max":
            count = min(count, max(1, math.ceil(int(quota) / int(period))))
    quota = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "")
    period = read("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "")
    if quota.lstrip("-").isdigit() and int(quota) > 0 and period.isdigit():
        count = min(count, max(1, math.ceil(int(quota) / int(period))))
    return count


def cpu_info():
    section("CPU")
    if shutil.which("lscpu"):
        code, out = run(["lscpu"])
        keys = ("Architecture:", "CPU(s):", "Model name:", "Thread(s)", "Core(s)",
                "Socket(s)", "CPU MHz:", "CPU max MHz:", "Hypervisor vendor:", "Virtualization type:")
        say("\n".join(x for x in out.splitlines() if x.startswith(keys)))
        if code:
            say("lscpu failed; see /proc/cpuinfo below")
    else:
        info = read("/proc/cpuinfo", "")
        say(next((x for x in info.splitlines() if x.startswith(("model name", "Hardware"))), platform.machine()))
    say("Effective CPU workers (affinity/quota): {}".format(cpu_count()))
    say("Load average: " + read("/proc/loadavg"))
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        raw = read(zone / "temp", "")
        if raw.lstrip("-").isdigit():
            temps.append("{}: {:.1f} C".format(read(zone / "type"), int(raw) / 1000))
    say("Temperature: " + (", ".join(temps) if temps else "not exposed by host"))


def memory_info():
    section("MEMORY")
    data = meminfo()
    for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
        say("{}: {:.1f} MiB".format(key, data.get(key, 0) / 1048576))
    say("Available after cgroup limit: {:.1f} MiB".format(available_memory() / 1048576))
    say("VPS memory type / slots / physical ECC state may be hidden by the host.")


def disk_info():
    section("DISK / FILESYSTEM")
    if shutil.which("lsblk"):
        show_command(["lsblk", "-o", "NAME,TYPE,SIZE,ROTA,MODEL,MOUNTPOINT"])
    if shutil.which("df"):
        show_command(["df", "-hT", "-x", "tmpfs", "-x", "devtmpfs"])
    say("Read-only inventory. SMART/physical wear are usually hidden on virtual disks.")


def network_info(args):
    section("NETWORK QUALITY")
    say("External probes contact configured hosts. ICMP filtering is not proof of a broken service.")
    if shutil.which("ip"):
        show_command(["ip", "-brief", "address"])
        show_command(["ip", "route", "show", "default"])
        show_command(["ip", "-6", "route", "show", "default"])
    for family in ([4, 6] if args.family == "both" else [int(args.family)]):
        for target in args.target:
            try:
                if ipaddress.ip_address(target).version != family:
                    continue
            except ValueError:
                pass
            say("\nIPv{} target {} (ICMP loss / min / avg / max / jitter):".format(family, target))
            if shutil.which("ping"):
                show_command(["ping", "-" + str(family), "-n", "-c", str(args.ping_count),
                              "-W", "2", "-w", str(args.ping_count * 3 + 3), target], args.ping_count * 3 + 5)
            else:
                say("[SKIP] ping missing (iputils-ping / iputils)")
            if args.route:
                if shutil.which("mtr"):
                    say("Outbound TCP/443 MTR; intermediate loss may be ICMP rate limiting.")
                    show_command(["mtr", "-" + str(family), "-n", "-r", "-w", "-c", "3",
                                  "-m", "20", "-T", "-P", "443", target], 45)
                else:
                    say("[SKIP] mtr missing; install mtr / mtr-tiny")
    if shutil.which("curl"):
        for family in ([4, 6] if args.family == "both" else [int(args.family)]):
            say("\nHTTPS IPv{}: {}".format(family, args.http_url))
            show_command(["curl", "-q", "-" + str(family), "--noproxy", "*", "--proto", "=https",
                          "--silent", "--show-error", "--output", "/dev/null", "--head",
                          "--connect-timeout", "5", "--max-time", "12", "--write-out",
                          "HTTP=%{http_code} DNS=%{time_namelookup}s TCP=%{time_connect}s "
                          "TLS=%{time_appconnect}s TTFB=%{time_starttransfer}s total=%{time_total}s\n",
                          args.http_url], 15)
    else:
        say("[SKIP] curl missing; HTTPS timing unavailable")
    if args.download_url:
        download_test(args)
    say("\nNo reverse-route / carrier / IP reputation score is inferred from these probes.")


def download_test(args):
    section("BOUNDED DOWNLOAD SAMPLE")
    if not shutil.which("curl"):
        say("[SKIP] curl missing")
        return
    say("Source: {} | max payload: {} MiB | deadline: 20s".format(args.download_url, args.download_mib))
    # Pipe into a bounded reader. Never buffer payload or save it on disk. A
    # server ignoring Range is still cut off at exactly the requested payload.
    command = ["curl", "-q", "--noproxy", "*", "--proto", "=https", "--fail", "--silent",
               "--connect-timeout", "5", "--max-time", "20", "--range",
               "0-{}".format(args.download_mib * 1048576 - 1), args.download_url]
    start = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=ENV, start_new_session=True)
    count = 0
    try:
        limit = args.download_mib * 1048576
        while count < limit:
            chunk = proc.stdout.read(min(65536, limit - count))
            if not chunk:
                break
            count += len(chunk)
        elapsed = time.monotonic() - start
        if count == limit:
            stop_process(proc)
            code = 0
        else:
            code = proc.wait(timeout=3)
        if code or not count:
            say("[FAILED] download exit={} bytes={}".format(code, count))
        else:
            say("Received {:.2f} MiB in {:.3f}s = {:.2f} Mbps (single connection incl. setup)".format(
                count / 1048576, elapsed, count * 8 / max(elapsed, .001) / 1000000))
        say("This small sample is not a saturated link or upload speed measurement.")
    finally:
        stop_process(proc)
        proc.stdout.close()


def stress_test(args):
    section("OPT-IN STRESS TEST")
    if not shutil.which("stress-ng"):
        say("[SKIP] stress-ng missing; rerun with --install-deps as root")
        return
    for item in args.stress:
        cmd = ["stress-ng", "--timeout", "{}s".format(args.duration), "--metrics-brief", "--verify"]
        if item == "cpu":
            workers = min(args.workers or cpu_count(), cpu_count())
            cmd += ["--cpu", str(workers), "--cpu-load", str(args.cpu_load)]
            say("CPU: {} workers, {}% load, {}s".format(workers, args.cpu_load, args.duration))
        else:
            available = available_memory()
            budget = min(args.memory_mib * 1048576, int(available * .25), max(0, available - 256 * 1048576))
            if budget < 16 * 1048576:
                say("[SKIP] memory: insufficient headroom (256 MiB reserved)")
                continue
            cmd += ["--vm", "1", "--vm-bytes", str(budget), "--vm-keep"]
            say("Memory: {:.0f} MiB, {}s".format(budget / 1048576, args.duration))
        code = show_command(cmd, args.duration + 15)
        say("Stress {}: {} (not a hardware certification)".format(item, "PASS" if code == 0 else "FAILED"))


def disk_benchmark(args):
    section("OPT-IN DISK BENCHMARK")
    if not shutil.which("fio"):
        say("[SKIP] fio missing; rerun with --install-deps as root")
        return
    code, version = run(["fio", "--version"])
    if code or not re.match(r"fio-\d", version):
        say("[SKIP] installed fio is not the Flexible I/O Tester")
        return
    target = Path(args.disk_dir).expanduser().resolve(strict=True)
    if not target.is_dir():
        raise ValueError("--disk-dir must be an existing directory, never a raw device")
    size = args.disk_mib * 1048576
    free = shutil.disk_usage(target).free
    if free < size + 1024 * 1048576:
        say("[SKIP] disk benchmark requires test size + 1 GiB free headroom")
        return
    say("Directory: {} | test file: {} MiB | 4 phases x {}s".format(target, args.disk_mib, args.duration))
    say("Tests write a unique disposable regular file; no raw device is opened.")
    with tempfile.TemporaryDirectory(prefix=".vps-inspector-", dir=str(target)) as folder:
        filename = str(Path(folder) / "test.bin")
        # Fully initialize before timed reads, avoiding sparse-file/zero-hole results.
        init = ["fio", "--name=prepare", "--filename=" + filename, "--size=" + str(size),
                "--rw=write", "--bs=1M", "--direct=1", "--ioengine=psync", "--iodepth=1",
                "--fallocate=none", "--end_fsync=1", "--output-format=json", "--eta=never"]
        code, out = run(init, 120)
        if code:
            say("[FAILED] test-file initialization (120s limit): " + out[:1500])
            return
        for mode, block in (("write", "1M"), ("read", "1M"), ("randwrite", "4k"), ("randread", "4k")):
            cmd = ["fio", "--name=" + mode, "--filename=" + filename, "--size=" + str(size),
                   "--rw=" + mode, "--bs=" + block, "--direct=1", "--ioengine=psync", "--iodepth=1",
                   "--numjobs=1", "--runtime=" + str(args.duration), "--time_based=1", "--group_reporting",
                   "--output-format=json", "--eta=never", "--fallocate=none", "--end_fsync=1"]
            code, out = run(cmd, args.duration + 30)
            try:
                data = json.loads(out)
                job = data["jobs"][0]
                if code or job.get("error"):
                    raise ValueError("fio exit={} job error={}".format(code, job.get("error")))
                kind = "read" if "read" in mode else "write"
                result = job[kind]
                say("{} {}: {:.2f} MiB/s | {:.0f} IOPS | mean latency {:.3f} ms".format(
                    mode, block, result.get("bw_bytes", result.get("bw", 0) * 1024) / 1048576,
                    result.get("iops", 0), result.get("clat_ns", {}).get("mean", 0) / 1000000))
            except (ValueError, KeyError, IndexError) as exc:
                say("[FAILED] {}: {}\n{}".format(mode, exc, out[:1500]))
                break
    say("Temporary test file removed. QD1 results reflect the chosen filesystem and host cache policy.")


def save_png(filename):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError("PNG requires Pillow: apt install python3-pil fonts-dejavu-core (or --install-deps)")
    import textwrap
    fonts = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
             "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
             "/usr/share/fonts/TTF/DejaVuSansMono.ttf"]
    font = next((ImageFont.truetype(p, 16) for p in fonts if Path(p).exists()), ImageFont.load_default())
    lines = []
    for line in LINES:
        lines.extend(textwrap.wrap(line, width=115, replace_whitespace=False, drop_whitespace=False) or [""])
    width, line_height = 1160, 24
    if len(lines) > 2500:
        raise ValueError("Report too long for PNG (2500 lines max)")
    img = Image.new("RGB", (width, 64 + line_height * len(lines)), "#101827")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((24, 24 + i * line_height), line, font=font, fill="#dbeafe")
    path = Path(filename).expanduser().absolute()
    # Refuse overwrite and symlinks, private file permissions, no text intermediates.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            img.save(handle, format="PNG")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print("PNG saved locally: {}".format(path), flush=True)


def install_dependencies():
    if os.geteuid() != 0:
        raise ValueError("--install-deps requires root; otherwise preinstall dependencies yourself")
    if shutil.which("apt-get"):
        commands = [["apt-get", "update"], ["apt-get", "install", "-y", "python3", "curl", "iproute2",
                    "iputils-ping", "util-linux", "stress-ng", "fio", "mtr-tiny", "python3-pil", "fonts-dejavu-core"]]
    elif shutil.which("dnf"):
        commands = [["dnf", "install", "-y", "python3", "curl", "iproute", "iputils", "util-linux", "stress-ng",
                     "fio", "mtr", "python3-pillow", "dejavu-sans-mono-fonts"]]
    elif shutil.which("apk"):
        commands = [["apk", "add", "python3", "curl", "iproute2", "iputils", "util-linux", "stress-ng",
                     "fio", "mtr", "py3-pillow", "font-dejavu"]]
    else:
        raise ValueError("Automatic install supports apt-get / dnf / apk; install tools manually on other distros")
    for command in commands:
        code, out = run(command, 300)
        print(out, flush=True)
        if code:
            raise ValueError("Dependency install failed; package availability depends on distribution repositories")


def csv_choice(value, choices):
    parts = value.split(",")
    if not parts or any(p not in choices for p in parts):
        raise argparse.ArgumentTypeError("choose comma-separated values from " + ",".join(choices))
    return list(dict.fromkeys(parts))


def integer_between(low, high):
    def parse(value):
        try:
            result = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected an integer")
        if not low <= result <= high:
            raise argparse.ArgumentTypeError("expected {}..{}".format(low, high))
        return result
    return parse


def host(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if len(value) <= 253 and re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value):
            return value
    raise argparse.ArgumentTypeError("expected hostname or IP, without port / URL / options")


def https_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or any(c.isspace() or ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError("expected HTTPS URL without credentials or whitespace")
    return value


def parser():
    p = argparse.ArgumentParser(description="VPS Inspector: CPU/memory/disk/network; stdout only unless --png.")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("-N", "--interactive", action="store_true", help="interactive module/test/output selection")
    p.add_argument("--modules", type=lambda x: csv_choice(x, MODULES), default=list(MODULES))
    p.add_argument("--stress", type=lambda x: csv_choice(x, ("cpu", "memory")), default=[], help="explicit CPU/memory load")
    p.add_argument("--duration", type=integer_between(1, 3600), default=15)
    p.add_argument("--workers", type=integer_between(1, 256))
    p.add_argument("--cpu-load", type=integer_between(1, 100), default=75)
    p.add_argument("--memory-mib", type=integer_between(16, 1048576), default=256)
    p.add_argument("--disk-bench", action="store_true", help="explicit disposable-file read/write benchmark")
    p.add_argument("--disk-dir", default=".")
    p.add_argument("--disk-mib", type=integer_between(64, 16384), default=256)
    p.add_argument("--target", type=host, action="append", help="repeat to replace default ping/route hosts")
    p.add_argument("--family", choices=("4", "6", "both"), default="both")
    p.add_argument("-4", dest="family", action="store_const", const="4")
    p.add_argument("-6", dest="family", action="store_const", const="6")
    p.add_argument("--ping-count", type=integer_between(1, 30), default=5)
    p.add_argument("--route", action="store_true", help="outbound TCP/443 MTR, not reverse route")
    p.add_argument("--http-url", type=https_url, default="https://www.cloudflare.com/")
    p.add_argument("--download-url", type=https_url, help="explicit download sample against this HTTPS source")
    p.add_argument("--download-mib", type=integer_between(1, 256), default=10)
    p.add_argument("--png", metavar="PATH", help="save report PNG locally; never overwrite")
    p.add_argument("--install-deps", action="store_true", help="explicit OS package installation (root)")
    return p


def interactive(args, p):
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8", buffering=1)
    except OSError:
        p.error("-N needs a terminal; use --modules / --stress / --disk-bench / --png in automation")
    def ask(prompt, default):
        tty.write(prompt + " [" + default + "]: ")
        answer = tty.readline()
        if not answer:
            raise ValueError("Terminal closed")
        return answer.strip() or default
    with tty:
        tty.write("VPS Inspector 交互模式：默认不压测、不写测试文件、不保存报告。\n")
        try:
            args.modules = csv_choice(ask("检测模块 cpu,memory,disk,network", ",".join(args.modules)), MODULES)
            chosen = ask("压力测试 none/cpu/memory/cpu,memory（会占用资源）", ",".join(args.stress) or "none")
            args.stress = [] if chosen == "none" else csv_choice(chosen, ("cpu", "memory"))
            args.disk_bench = ask("硬盘写入测试 y/n", "y" if args.disk_bench else "n").lower() == "y"
            if args.disk_bench:
                args.disk_dir = ask("测试目录（需额外 1 GiB 空间）", args.disk_dir)
            if args.stress or args.disk_bench:
                args.duration = integer_between(1, 3600)(ask("每项测试秒数（硬盘共4项）", str(args.duration)))
            if "network" in args.modules:
                args.route = ask("去程 TCP MTR y/n", "y" if args.route else "n").lower() == "y"
                targets = ask("延迟/路由目标，逗号分隔", ",".join(args.target or ["1.1.1.1", "2606:4700:4700::1111"]))
                args.target = [host(x.strip()) for x in targets.split(",")]
                args.family = ask("IP 版本 4/6/both", args.family)
                if args.family not in ("4", "6", "both"):
                    raise ValueError("IP version must be 4/6/both")
                speed = ask("下载测速 HTTPS 地址，none 跳过（消耗流量）", args.download_url or "none")
                args.download_url = None if speed == "none" else https_url(speed)
            output = ask("保存 PNG 路径，none 仅终端文字", args.png or "none")
            args.png = None if output == "none" else output
        except argparse.ArgumentTypeError as exc:
            p.error(str(exc))


def validate(args, p):
    if any(x not in args.modules for x in args.stress):
        p.error("each --stress module must also be included in --modules")
    if args.disk_bench and "disk" not in args.modules:
        p.error("--disk-bench requires disk in --modules")
    if (args.route or args.download_url) and "network" not in args.modules:
        p.error("--route / --download-url require network in --modules")
    if args.png:
        path = Path(args.png).expanduser()
        if path.exists() or path.is_symlink():
            p.error("PNG path already exists (overwriting is disabled)")
        if not path.parent.is_dir():
            p.error("PNG parent directory must already exist")
    if args.disk_bench and not Path(args.disk_dir).expanduser().is_dir():
        p.error("--disk-dir must be an existing directory")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    LINES.clear()
    if platform.system() != "Linux":
        p.error("Run on a Linux VPS (Python 3.8+), not on the Windows host")
    if args.interactive:
        interactive(args, p)
    validate(args, p)
    if args.install_deps:
        install_dependencies()
    if args.png:
        try:
            import PIL  # noqa: F401 -- fail before expensive tests if PNG unavailable
        except ImportError:
            p.error("PNG requires Pillow; use --install-deps or install python3-pil")
    if args.target is None:
        args.target = ["one.one.one.one", "dns.google"]
    section("VPS INSPECTOR " + VERSION)
    say("Time: " + datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except (AttributeError, OSError):
        os_name = "Linux"
    say("OS: {} | Kernel: {} | Arch: {}".format(os_name, platform.release(), platform.machine()))
    say("Modules: " + ", ".join(args.modules))
    say("Reports stay in memory; no telemetry or report upload. Addresses may appear below.")
    for item in args.modules:
        try:
            {"cpu": cpu_info, "memory": memory_info, "disk": disk_info,
             "network": lambda: network_info(args)}[item]()
        except (OSError, ValueError) as exc:
            say("[FAILED] {}: {}".format(item, exc))
    if args.stress:
        stress_test(args)
    if args.disk_bench:
        disk_benchmark(args)
    section("END")
    say("Report not saved as text. Optional benchmarks only run when explicitly selected.")
    if args.png:
        save_png(args.png)
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; child processes stopped; temporary test directory cleaned.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
