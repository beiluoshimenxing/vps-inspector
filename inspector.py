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
import unicodedata

VERSION = "1.1.0"
MODULES = ("cpu", "memory", "disk", "network")
LINES = []
ENV = dict(os.environ, LC_ALL="C", LANG="C")
ROWS = []
SUMMARY = {}
ISSUES = []
VERBOSE = False
COLOR = "auto"
STARTED = 0
SECTION_NUMBER = 0
PALETTE = {"accent": "96", "title": "1;97", "muted": "90", "ok": "92", "warn": "93", "bad": "91", "skip": "93"}
LABELS = {
    "CPU": "处理器", "MEMORY": "内存", "DISK / FILESYSTEM": "硬盘与文件系统",
    "NETWORK QUALITY": "网络质量", "OPT-IN STRESS TEST": "压力测试",
    "OPT-IN DISK BENCHMARK": "硬盘性能", "BOUNDED DOWNLOAD SAMPLE": "下载测速",
    "SUMMARY": "检测摘要", "Model": "处理器型号", "Topology": "可见拓扑",
    "Effective CPUs": "可用 CPU", "Architecture": "架构", "Virtualization": "虚拟化",
    "Load average": "系统负载", "Temperature": "温度", "Physical memory": "可见内存",
    "Available": "可用内存", "Memory usage": "内存使用", "Swap": "交换空间",
    "Cgroup headroom": "容器可用预算", "System": "操作系统", "Kernel / arch": "内核 / 架构",
    "Selected": "检测项目", "Time": "检测时间", "Interface": "网卡地址",
    "Default route": "默认网关", "Test file": "测试文件", "Disk space": "空间检查",
    "Stress CPU": "CPU 压测", "Stress memory": "内存压测", "Disk benchmark": "硬盘测试",
    "Sequential write": "顺序写入", "Sequential read": "顺序读取", "Random write 4K": "随机写入 4K",
    "Random read 4K": "随机读取 4K", "Download": "下载样本", "Output": "报告输出",
    "Elapsed": "检测耗时", "Disk": "磁盘容量", "Network": "网络探测", "Attention": "需要注意",
    "Stress tests": "压力测试", "Source": "采样来源",
}


def clean(value):
    return "".join(c if c == "\n" or (c.isprintable() and c != "\x7f") else "?" for c in str(value))


def cells(value):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in str(value))


def fit(value, width):
    value = clean(value).replace("\n", " ")
    if cells(value) <= width:
        return value
    result = ""
    for char in value:
        if cells(result + char) > width - 1:
            return result.rstrip() + "…"
        result += char
    return result


def pad(value, width):
    return value + " " * max(0, width - cells(value))


def frame(title):
    width = max(36, min(76, shutil.get_terminal_size((82, 24)).columns - 6))
    return ["  ╭" + "─" * width + "╮", "  │" + pad("  " + fit(title, width - 3), width) + "│", "  ╰" + "─" * width + "╯"]


def colored(value, style, stream=None):
    stream = stream or sys.stdout
    enabled = COLOR == "always" or (COLOR == "auto" and stream.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ)
    return "\033[{}m{}\033[0m".format(PALETTE.get(style, "0"), value) if enabled else value


def emit(value="", style=None, kind="text", english=None, label=None, status=None):
    value = clean(value)
    for line in value.split("\n"):
        LINES.append(line)
        print(colored(line, style) if style else line, flush=True)
    ROWS.append(dict(kind=kind, text=value, english=clean(english if english is not None else value),
                     label=label, status=status, style=style))


def metric(label, value, status=None, english=None):
    value = clean(value).replace("\n", " ")
    name = fit(LABELS.get(label, label), 16)
    badge = {"ok": "[ OK ]", "warn": "[注意]", "bad": "[失败]", "skip": "[跳过]"}.get(status, "")
    width = max(40, min(100, shutil.get_terminal_size((88, 24)).columns - 6))
    display_value = fit(value, width - 22 - (8 if badge else 0))
    line = "  " + pad(name, 16) + "  " + display_value
    if badge:
        line += "  " + badge
    # Color values/badges separately so labels remain quiet.
    LINES.append(line)
    print("  " + colored(pad(name, 16), "muted") + "  " + colored(display_value + ("  " + badge if badge else ""), status if status in PALETTE else "title"), flush=True)
    ROWS.append(dict(kind="metric", text=line, english=clean(english or value), label=label, status=status, style=status))


def note(text, english=None, status=None):
    width = max(36, min(98, shutil.get_terminal_size((88, 24)).columns - 6))
    lines, current = [], ""
    for char in clean(text).replace("\n", " "):
        if cells(current + char) > width:
            lines.append("  " + current)
            current = ""
        current += char
    lines.append("  " + current)
    emit("\n".join(lines), "warn" if status else "muted", "note", english or text, status=status)


def issue(label, text, english=None, failed=False):
    ISSUES.append((label, text, english or text))
    metric(label, text, "bad" if failed else "skip", english)


def raw_output(text):
    if VERBOSE and text:
        emit("  ┆ 原始输出 / RAW", "muted", "detail", "RAW OUTPUT")
        for line in text.splitlines():
            emit("  ┆ " + line, "muted", "detail", line)


def progress(message):
    if sys.stderr.isatty():
        print(colored("  ◌ " + clean(message), "muted", sys.stderr), file=sys.stderr, flush=True)


def say(value=""):
    # Escape terminal controls from hostnames, command output and remote responses.
    emit(value)


def section(title):
    global SECTION_NUMBER
    SECTION_NUMBER += 1
    width = max(40, min(86, shutil.get_terminal_size((88, 24)).columns - 2))
    emit("", kind="space")
    emit("  {:02d}  {}  /  {}".format(SECTION_NUMBER, LABELS.get(title, title), title), "accent", "section", title)
    emit("  " + "─" * (width - 4), "muted", "rule", "")


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
    raw_output(out)
    if code:
        issue(cmd[0], "命令执行失败（exit={}），详细模式可查看原因".format(code), "Command failed: exit={}; use --verbose".format(code), True)
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
    fields = {}
    if shutil.which("lscpu"):
        code, out = run(["lscpu"])
        if code == 0:
            fields = dict(x.split(":", 1) for x in out.splitlines() if ":" in x)
            fields = {k.strip(): v.strip() for k, v in fields.items()}
        raw_output(out)
    model = fields.get("Model name", "")
    if not model:
        info = read("/proc/cpuinfo", "")
        model = next((x.split(":", 1)[-1].strip() for x in info.splitlines() if x.startswith(("model name", "Hardware"))), platform.machine())
    workers = cpu_count()
    SUMMARY["CPU"] = "{} vCPU · {}".format(workers, model)
    metric("Model", model)
    metric("Effective CPUs", "{} vCPU".format(workers), "ok")
    metric("Topology", "{} CPU / {} cores per socket / {} threads per core".format(
        fields.get("CPU(s)", "?"), fields.get("Core(s) per socket", "?"), fields.get("Thread(s) per core", "?")))
    metric("Architecture", fields.get("Architecture", platform.machine()))
    metric("Virtualization", fields.get("Hypervisor vendor", "Not exposed") + " / " + fields.get("Virtualization type", "unknown"))
    metric("Load average", " / ".join(read("/proc/loadavg").split()[:3]) + "  (1 / 5 / 15 min)")
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        raw = read(zone / "temp", "")
        if raw.lstrip("-").isdigit():
            temps.append("{}: {:.1f} C".format(read(zone / "type"), int(raw) / 1000))
    metric("Temperature", ", ".join(temps) if temps else "宿主机未暴露", english=", ".join(temps) if temps else "Not exposed by host")


def memory_info():
    section("MEMORY")
    data = meminfo()
    total, available = data.get("MemTotal", 0), data.get("MemAvailable", 0)
    used = max(0, total - available)
    ratio = used / max(1, total)
    status = "bad" if ratio >= .95 else "warn" if ratio >= .85 else "ok"
    bar = "━" * round(ratio * 20) + "·" * (20 - round(ratio * 20))
    metric("Physical memory", "{:.1f} MiB".format(total / 1048576))
    metric("Available", "{:.1f} MiB".format(available / 1048576), status)
    metric("Memory usage", "{}  {:.1f}%".format(bar, ratio * 100), status,
           "{:.1f}% used | {:.1f} MiB available".format(ratio * 100, available / 1048576))
    metric("Swap", "{:.1f} MiB total / {:.1f} MiB free".format(data.get("SwapTotal", 0) / 1048576, data.get("SwapFree", 0) / 1048576))
    metric("Cgroup headroom", "{:.1f} MiB".format(available_memory() / 1048576))
    SUMMARY["MEMORY"] = "{:.0f} MiB / {:.1f}% used".format(total / 1048576, ratio * 100)
    if ratio >= .85:
        ISSUES.append(("MEMORY", "内存占用较高，请留意压测预算", "High memory use; check stress-test budget"))
    note("内存占用按总量减可用量估算；VPS 通常无法读取物理槽位与 ECC。", "Usage = total - available; physical slots / ECC usually hidden on VPS.")


def disk_info():
    section("DISK / FILESYSTEM")
    if shutil.which("lsblk"):
        code, out = run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,ROTA,MODEL,MOUNTPOINT"])
        try:
            devices = json.loads(out)["blockdevices"]
            for device in devices:
                if device.get("type") == "disk":
                    size = int(device.get("size") or 0) / 1024**3
                    # ROTA alone does not prove SSD backing on a virtual disk.
                    metric(str(device.get("name")), "{:.2f} GiB · {} · ROTA={}".format(size, str(device.get("model") or "unknown").strip(), device.get("rota")))
        except (ValueError, KeyError, TypeError):
            issue("Disk", "设备信息解析失败，详细模式可查看原始输出", "Cannot parse lsblk; use --verbose")
        raw_output(out)
    if shutil.which("df"):
        code, out = run(["df", "-PT", "-B1", "-x", "tmpfs", "-x", "devtmpfs"])
        if code:
            issue("Disk", "文件系统容量读取失败", "df failed; filesystem capacity unavailable", True)
        for line in out.splitlines()[1:]:
            parts = line.split(None, 6)
            if len(parts) != 7:
                continue
            device, fs, size, used, free, percent, mount = parts
            try:
                pct = int(percent.rstrip("%"))
                status = "bad" if pct >= 95 else "warn" if pct >= 85 else "ok"
                value = "{} · {:.1f} GiB free / {:.1f} GiB · {} used".format(fs, int(free) / 1024**3, int(size) / 1024**3, percent)
                metric(mount, value, status)
                if mount == "/":
                    SUMMARY["Disk"] = "{:.1f} GiB / {} used".format(int(size) / 1024**3, percent)
                if pct >= 85:
                    ISSUES.append(("Disk", mount + " 空间使用率较高", "High filesystem usage: " + mount))
            except ValueError:
                raw_output(line)
        raw_output(out)
    else:
        issue("Disk", "缺少 df，无法读取文件系统容量", "df missing; filesystem capacity unavailable")
    note("ROTA 是虚拟设备属性，不能据此判定宿主盘类型；SMART 通常不可见。", "ROTA is a virtual-device hint, not proof of SSD/HDD backing. SMART usually hidden.")


def parse_ping(out):
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"(?:rtt|round-trip).*?=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
    return (float(loss.group(1)) if loss else None, [float(v) for v in rtt.groups()] if rtt else None)


def network_info(args):
    section("NETWORK QUALITY")
    reachable, probes = 0, 0
    if shutil.which("ip"):
        code, out = run(["ip", "-brief", "address"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] != "lo":
                metric("Interface", parts[0] + "  " + "  ".join(p for p in parts[2:] if not p.startswith("fe80:")))
        raw_output(out)
    for family in ([4, 6] if args.family == "both" else [int(args.family)]):
        for target in args.target:
            try:
                if ipaddress.ip_address(target).version != family:
                    continue
            except ValueError:
                pass
            probes += 1
            label = "IPv{} {}".format(family, target)
            note("IPv{} → {}".format(family, target))
            if shutil.which("ping"):
                progress("测量 IPv{} → {} 的延迟与丢包…".format(family, target))
                code, out = run(["ping", "-" + str(family), "-n", "-c", str(args.ping_count),
                                "-W", "2", "-w", str(args.ping_count * 3 + 3), target], args.ping_count * 3 + 5)
                loss, rtt = parse_ping(out)
                if rtt is not None and loss is not None:
                    reachable += 1
                    status = "ok" if loss == 0 else "warn"
                    metric("ICMP IPv" + str(family), "AVG {:.2f} ms · LOSS {:g}% · JITTER {:.2f} ms".format(rtt[1], loss, rtt[3]), status)
                    if loss > 0:
                        ISSUES.append((label, "ICMP 样本出现丢包，需结合业务连接判断", "ICMP sample loss observed; verify application traffic"))
                else:
                    issue(label, "无 ICMP 响应 / 可能被过滤", "No ICMP reply / may be filtered")
                raw_output(out)
            else:
                issue(label, "缺少 ping", "ping missing (iputils-ping / iputils)")
            if args.route:
                if shutil.which("mtr"):
                    progress("追踪 IPv{} → {} 的去程路由…".format(family, target))
                    code, out = run(["mtr", "-" + str(family), "-n", "-r", "-w", "-c", "3",
                                     "-m", "20", "-T", "-P", "443", target], 45)
                    hops = [x.strip() for x in out.splitlines() if re.match(r"\s*\d+\.\|", x)]
                    if code == 0 and hops:
                        metric("MTR IPv" + str(family), "{} hops · TCP/443 → {}".format(len(hops), target))
                        # Keep route endpoints in compact view; full hop table in verbose mode.
                        note("终点采样：" + hops[-1], "Final sampled hop: " + hops[-1])
                    else:
                        issue("MTR IPv" + str(family), "路由采样不可用（权限或目标限制）", "MTR unavailable (permissions / target restrictions)")
                    raw_output(out)
                else:
                    issue("MTR", "缺少 mtr，已跳过路由采样", "mtr missing; install mtr / mtr-tiny")
    if shutil.which("curl"):
        note("HTTPS 目标：" + args.http_url, "HTTPS endpoint: " + args.http_url)
        for family in ([4, 6] if args.family == "both" else [int(args.family)]):
            progress("测量 HTTPS IPv{} 建连与首字节…".format(family))
            code, out = run(["curl", "-q", "-" + str(family), "--noproxy", "*", "--proto", "=https",
                          "--silent", "--show-error", "--output", "/dev/null", "--head",
                          "--connect-timeout", "5", "--max-time", "12", "--write-out",
                          "HTTP=%{http_code} DNS=%{time_namelookup}s TCP=%{time_connect}s "
                          "TLS=%{time_appconnect}s TTFB=%{time_starttransfer}s total=%{time_total}s\n",
                          args.http_url], 15)
            timings = dict(re.findall(r"(HTTP|DNS|TCP|TLS|TTFB|total)=([\d.]+)", out))
            if code == 0 and timings.get("HTTP", "000") != "000":
                status_code = int(timings["HTTP"])
                status = "ok" if 200 <= status_code < 400 else "warn"
                if status == "warn":
                    ISSUES.append(("HTTPS IPv" + str(family), "目标返回 HTTP " + str(status_code), "Endpoint returned HTTP " + str(status_code)))
                metric("HTTPS IPv" + str(family), "HTTP={} · TTFB {:.1f} ms · TOTAL {:.1f} ms".format(
                    status_code, float(timings.get("TTFB", 0)) * 1000, float(timings.get("total", 0)) * 1000), status)
                dns = float(timings.get("DNS", 0))
                tcp = float(timings.get("TCP", 0))
                tls = float(timings.get("TLS", 0))
                note("DNS {:.1f} ms  /  TCP {:.1f} ms  /  TLS {:.1f} ms".format(
                    dns * 1000, max(0, tcp - dns) * 1000, max(0, tls - tcp) * 1000))
            else:
                issue("HTTPS IPv" + str(family), "连接失败 / 当前协议栈可能不可用", "HTTPS connection failed; address family may be unavailable")
            raw_output(out)
    else:
        issue("HTTPS", "缺少 curl", "curl missing; HTTPS timing unavailable")
    if args.download_url:
        download_test(args)
    SUMMARY["Network"] = "{}/{} ICMP targets replied".format(reachable, probes)
    note("MTR 仅代表去程；中间节点限速不等于业务丢包。", "MTR is outbound only; intermediate ICMP rate limiting is not application loss.")


def download_test(args):
    section("BOUNDED DOWNLOAD SAMPLE")
    if not shutil.which("curl"):
        say("[SKIP] curl missing")
        return
    metric("Source", args.download_url)
    note("上限 {} MiB / 最长 20 秒；仅单连接下载采样。".format(args.download_mib), "Limit: {} MiB / 20s / single-connection sample".format(args.download_mib))
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
            issue("Download", "下载失败：exit={} / {} bytes".format(code, count), "Download failed: exit={} bytes={}".format(code, count), True)
        else:
            mbps = count * 8 / max(elapsed, .001) / 1000000
            metric("Download", "{:.2f} Mbps · Received {:.2f} MiB / {:.2f}s".format(mbps, count / 1048576, elapsed), "ok")
            SUMMARY["Download"] = "{:.2f} Mbps (bounded sample)".format(mbps)
        note("单连接限量采样，包含建连耗时，不代表链路极限带宽。", "Single-connection bounded sample, including setup; not link capacity.")
    finally:
        stop_process(proc)
        proc.stdout.close()


def stress_test(args):
    section("OPT-IN STRESS TEST")
    if not shutil.which("stress-ng"):
        issue("Stress tests", "缺少 stress-ng，本次压力测试未执行", "stress-ng missing; selected stress tests NOT RUN")
        note("安装：apt-get install stress-ng，或以 root 添加 --install-deps。", "Install stress-ng, or run as root with --install-deps.", "warn")
        return
    for item in args.stress:
        cmd = ["stress-ng", "--timeout", "{}s".format(args.duration), "--metrics-brief", "--verify"]
        if item == "cpu":
            workers = min(args.workers or cpu_count(), cpu_count())
            cmd += ["--cpu", str(workers), "--cpu-load", str(args.cpu_load)]
            config = "{} workers · {}% load · {}s".format(workers, args.cpu_load, args.duration)
        else:
            available = available_memory()
            budget = min(args.memory_mib * 1048576, int(available * .25), max(0, available - 256 * 1048576))
            if budget < 16 * 1048576:
                issue("Stress memory", "余量不足，保留 256 MiB 后无法压测", "Insufficient memory headroom; 256 MiB reserved")
                continue
            cmd += ["--vm", "1", "--vm-bytes", str(budget), "--vm-keep"]
            config = "{:.0f} MiB · {}s".format(budget / 1048576, args.duration)
        progress("正在进行 {} 压测：{}".format(item, config))
        code = show_command(cmd, args.duration + 15)
        result = "Stress {}: {} · {}".format(item, "PASS" if code == 0 else "FAILED", config)
        metric("Stress CPU" if item == "cpu" else "Stress memory", result, "ok" if code == 0 else "bad")
        SUMMARY["Stress " + item] = result
    note("PASS 仅表示所选测试完成，不等于硬件可靠性认证。", "PASS means the selected test completed, not hardware certification.")


def disk_benchmark(args):
    section("OPT-IN DISK BENCHMARK")
    if not shutil.which("fio"):
        issue("Disk benchmark", "缺少 fio，硬盘性能测试未执行", "fio missing; disk benchmark NOT RUN")
        return
    code, version = run(["fio", "--version"])
    if code or not re.match(r"fio-\d", version):
        issue("Disk benchmark", "fio 程序不匹配，已跳过", "Installed fio is not Flexible I/O Tester")
        return
    target = Path(args.disk_dir).expanduser().resolve(strict=True)
    if not target.is_dir():
        raise ValueError("--disk-dir must be an existing directory, never a raw device")
    size = args.disk_mib * 1048576
    free = shutil.disk_usage(target).free
    if free < size + 1024 * 1048576:
        issue("Disk benchmark", "空间不足：测试文件之外须预留 1 GiB", "Insufficient disk space: test size + 1 GiB required")
        return
    metric("Test file", "{} · {} MiB · 4 × {}s".format(target, args.disk_mib, args.duration))
    metric("Disk space", "{:.2f} GiB free · 1 GiB reserved".format(free / 1024**3), "ok")
    with tempfile.TemporaryDirectory(prefix=".vps-inspector-", dir=str(target)) as folder:
        filename = str(Path(folder) / "test.bin")
        # Fully initialize before timed reads, avoiding sparse-file/zero-hole results.
        init = ["fio", "--name=prepare", "--filename=" + filename, "--size=" + str(size),
                "--rw=write", "--bs=1M", "--direct=1", "--ioengine=psync", "--iodepth=1",
                "--fallocate=none", "--end_fsync=1", "--output-format=json", "--eta=never"]
        progress("初始化 {} MiB 临时测试文件…".format(args.disk_mib))
        code, out = run(init, 120)
        if code:
            issue("Disk benchmark", "初始化失败或超过 120 秒", "Test-file initialization failed / timed out", True)
            raw_output(out[:1500])
            return
        for mode, block in (("write", "1M"), ("read", "1M"), ("randwrite", "4k"), ("randread", "4k")):
            cmd = ["fio", "--name=" + mode, "--filename=" + filename, "--size=" + str(size),
                   "--rw=" + mode, "--bs=" + block, "--direct=1", "--ioengine=psync", "--iodepth=1",
                   "--numjobs=1", "--runtime=" + str(args.duration), "--time_based=1", "--group_reporting",
                   "--output-format=json", "--eta=never", "--fallocate=none", "--end_fsync=1"]
            progress("硬盘 {} / {}：{} 秒…".format(mode, block, args.duration))
            code, out = run(cmd, args.duration + 30)
            try:
                data = json.loads(out)
                job = data["jobs"][0]
                if code or job.get("error"):
                    raise ValueError("fio exit={} job error={}".format(code, job.get("error")))
                kind = "read" if "read" in mode else "write"
                result = job[kind]
                label = {"write": "Sequential write", "read": "Sequential read", "randwrite": "Random write 4K", "randread": "Random read 4K"}[mode]
                value = "{} {}: {:.2f} MiB/s | {:.0f} IOPS | {:.3f} ms".format(
                    mode, block, result.get("bw_bytes", result.get("bw", 0) * 1024) / 1048576,
                    result.get("iops", 0), result.get("clat_ns", {}).get("mean", 0) / 1000000)
                metric(label, value, "accent")
                SUMMARY[label] = value
            except (ValueError, KeyError, IndexError) as exc:
                issue("Disk benchmark", mode + " 测试失败：" + str(exc), mode + " failed: " + str(exc), True)
                raw_output(out[:1500])
                break
    note("临时文件已清理；QD1 结果受文件系统、缓存与宿主机策略影响。", "Temporary test file removed. QD1 reflects filesystem, cache and host policy.")


def save_png(filename):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError("PNG requires Pillow: apt install python3-pil fonts-dejavu-core (or --install-deps)")
    fonts = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
             "/usr/share/fonts/TTF/DejaVuSans.ttf", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"]
    fontpath = next((p for p in fonts if Path(p).exists()), None)
    def font(size):
        if fontpath:
            return ImageFont.truetype(fontpath, size)
        return ImageFont.load_default()
    regular, small, title, huge = font(20), font(16), font(27), font(39)
    def measure(text, f):
        return f.getlength(text) if hasattr(f, "getlength") else f.getsize(text)[0]
    def wrap(text, f, width):
        lines = []
        for paragraph in str(text).splitlines() or [""]:
            current = ""
            for char in paragraph:
                if current and measure(current + char, f) > width:
                    lines.append(current.rstrip())
                    current = ""
                current += char
            lines.append(current)
        return lines
    records = ROWS or [dict(kind="text", english=x, label=None, status=None) for x in LINES]
    blocks, current = [], None
    for row in records:
        kind = row["kind"]
        if kind in ("rule", "space", "hero"):
            continue
        if kind == "section":
            current = dict(title=row["english"], rows=[])
            blocks.append(current)
            continue
        if current is None:
            current = dict(title="SYSTEM OVERVIEW", rows=[])
            blocks.append(current)
        # Render full values, including values truncated to fit the terminal.
        text = row.get("english", "")
        label = row.get("label") if kind == "metric" else None
        lines = wrap(text, small if kind in ("note", "detail") else regular, 700 if label else 1050)
        label_lines = wrap(label, small, 270) if label else []
        height = max(42, len(lines) * 28 + 16, len(label_lines) * 28 + 16)
        current["rows"].append((row, lines, height))
    if sum(len(block["rows"]) for block in blocks) > 2500:
        raise ValueError("Report too long for PNG (2500 rows max)")
    header_height = 308 if SUMMARY else 172
    height = header_height + sum(80 + sum(r[2] for r in b["rows"]) + 22 for b in blocks) + 72
    if height > 50000:
        raise ValueError("Report too long for one PNG; disable --verbose")
    image = Image.new("RGB", (1200, height), "#090f1c")
    draw = ImageDraw.Draw(image)
    colors = {"ok": "#4ade80", "warn": "#fbbf24", "bad": "#fb7185", "skip": "#fbbf24"}
    draw.rectangle((0, 0, 1199, 5), fill="#38bdf8")
    draw.text((48, 30), "VPS INSPECTOR", font=huge, fill="#f1f5f9")
    draw.text((48, 88), "HARDWARE / NETWORK / PERFORMANCE", font=small, fill="#38bdf8")
    draw.rounded_rectangle((1030, 36, 1152, 73), radius=10, fill="#172b46")
    draw.text((1046, 43), "v" + VERSION, font=small, fill="#bae6fd")
    draw.text((48, 122), datetime.datetime.now().astimezone().isoformat(timespec="seconds") + "   |   LOCAL REPORT", font=small, fill="#94a3b8")
    if SUMMARY:
        for i, key in enumerate([k for k in ("CPU", "MEMORY", "Disk", "Network") if k in SUMMARY]):
            x = 48 + i * 280
            draw.rounded_rectangle((x, 170, x + 264, 282), radius=12, fill="#132137")
            draw.text((x + 18, 183), key.upper(), font=small, fill="#94a3b8")
            short = SUMMARY[key].split(" · ")[0].split(" / ")[0]
            if key == "Network":
                short = short.split(" ", 1)[0] + " replied"
            while measure(short, title) > 230 and len(short) > 4:
                short = short[:-2] + "…"
            draw.text((x + 18, 219), short, font=title, fill="#f8fafc")
    y = header_height
    for index, block in enumerate(blocks, 1):
        bh = 80 + sum(r[2] for r in block["rows"])
        draw.rounded_rectangle((32, y, 1168, y + bh), radius=14, fill="#111d30")
        draw.rectangle((32, y + 18, 36, y + 48), fill="#38bdf8")
        draw.text((54, y + 20), "{:02d}   {}".format(index, block["title"]), font=title, fill="#e0f2fe")
        row_y = y + 68
        for j, (row, lines, rh) in enumerate(block["rows"]):
            if j % 2 == 0:
                draw.rectangle((48, row_y, 1152, row_y + rh), fill="#142238")
            status = row.get("status")
            color = "#7dd3fc" if status == "accent" else colors.get(status, "#e2e8f0")
            if row.get("label"):
                for k, label_line in enumerate(wrap(row["label"], small, 270)):
                    draw.text((62, row_y + 9 + k * 28), label_line, font=small, fill="#94a3b8")
                x = 360
            else:
                x = 62
                color = "#94a3b8" if row["kind"] in ("note", "detail") else color
            f = small if row["kind"] in ("note", "detail") else regular
            for k, line in enumerate(lines):
                draw.text((x, row_y + 8 + k * 28), line, font=f, fill=color)
            if status in colors:
                draw.ellipse((1127, row_y + 16, 1137, row_y + 26), fill=colors.get(status, "#94a3b8"))
            row_y += rh
        y += bh + 22
    draw.text((48, height - 47), "Generated locally. No report upload.  /  github.com/beiluoshimenxing/vps-inspector", font=small, fill="#64748b")
    path = Path(filename).expanduser().absolute()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            image.save(handle, format="PNG")
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
    p.add_argument("--verbose", action="store_true", help="expand raw tool output below compact metrics")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="ANSI color policy; auto honors NO_COLOR")
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


def missing_tools(args):
    required = []
    if args.stress:
        required.append("stress-ng")
    if args.disk_bench:
        required.append("fio")
    if "network" in args.modules:
        required.extend(["ping", "curl"])
        if args.route:
            required.append("mtr")
    missing = [name for name in required if not shutil.which(name)]
    if args.png:
        try:
            import PIL  # noqa: F401
        except ImportError:
            missing.append("Pillow")
    return missing


def interactive(args, p):
    try:
        tty = open("/dev/tty", "r", encoding="utf-8")
        output = open("/dev/tty", "w", encoding="utf-8", buffering=1)
    except OSError:
        p.error("-N needs a terminal; use --modules / --stress / --disk-bench / --png in automation")

    feedback = []

    def write(text="", style=None):
        text = clean(text)
        output.write((colored(text, style, output) if style else text) + "\n")
        output.flush()

    def ask(prompt, default=""):
        output.write(colored("  › ", "accent", output) + clean(prompt) + (" [" + clean(default) + "]" if default else "") + ": ")
        output.flush()
        answer = tty.readline()
        if not answer:
            raise ValueError("Terminal closed")
        return answer.strip() or default

    def parsed(prompt, default, parse):
        while True:
            try:
                return parse(ask(prompt, str(default)))
            except (ValueError, argparse.ArgumentTypeError) as exc:
                write("  输入无效：" + str(exc), "warn")

    def yes(value):
        return colored("开启", "ok", output) if value else colored("关闭", "muted", output)

    def heading(title):
        write()
        for line in frame("VPS INSPECTOR   /   " + VERSION + "   /   " + title):
            write(line, "accent")

    def show_menu():
        if output.isatty() and os.environ.get("TERM") not in (None, "dumb"):
            # Only redraw the current interactive screen; never emit cursor codes to reports.
            output.write("\033[H\033[2J")
        heading("检测配置")
        write("     输入编号调整配置；直接回车开始。", "muted")
        write()
        for index, module in enumerate(MODULES, 1):
            selected = module in args.modules
            name = {"cpu": "CPU 处理器", "memory": "内存信息", "disk": "硬盘信息", "network": "网络质量"}[module]
            tail = {"cpu": "型号 / 核心 / 负载", "memory": "容量 / 可用 / Swap", "disk": "设备 / 挂载 / 容量", "network": "延迟 / 丢包 / HTTPS"}[module]
            write("  [{}] {} {}  {}".format(index, "●" if selected else "○", pad(name, 16), tail), "ok" if selected else "muted")
        write("  " + "─" * 66, "muted")
        stress = " + ".join({"cpu": "CPU", "memory": "内存"}[x] for x in args.stress) or "关闭"
        write("  [5] 压力测试        " + stress + "  /  {}s · CPU {}%".format(args.duration, args.cpu_load), "warn" if args.stress else "muted")
        # write raw styled strings here, never pass untrusted input into escapes.
        output.write("  [6] 硬盘写入测试    " + yes(args.disk_bench) + "  /  {} MiB · {}s × 4\n".format(args.disk_mib, args.duration))
        output.write("  [7] 网络设置        IPv{}  /  MTR {}  /  下载 {}\n".format(
            "4+6" if args.family == "both" else args.family, yes(args.route), yes(bool(args.download_url))))
        write("  [8] 报告保存        " + (fit(clean(args.png), 45) if args.png else "仅终端文字，不保存文件"))
        write("  [9] 压测参数        时长 / CPU 负载 / 内存预算 / 线程")
        write("  [v] 原始日志        " + ("展开（详细模式）" if args.verbose else "折叠（重点摘要）"), "muted")
        missing = missing_tools(args)
        if missing:
            write()
            write("  ! 缺少依赖：" + ", ".join(missing), "warn")
            write("    " + ("本次已指定 --install-deps，将在开始前安装。" if args.install_deps else "相关检测将跳过；可先用 --install-deps 安装。"), "warn")
        for message in feedback:
            write("  ! " + message, "warn")
        feedback.clear()
        write()
        write("  [Enter / r] 开始检测                         [q] 退出", "accent")

    def network_menu():
        if "network" not in args.modules:
            args.modules.append("network")
        while True:
            heading("网络设置")
            write("  [1] 去程 TCP MTR    " + ("开启" if args.route else "关闭"))
            write("  [2] IP 版本         " + args.family)
            write("  [3] 探测目标        " + ", ".join(args.target or ["one.one.one.one", "dns.google"]))
            write("  [4] 下载测速        " + (args.download_url or "关闭"))
            write("  [0 / Enter] 返回")
            choice = ask("选择", "0").lower()
            if choice == "0":
                return
            if choice == "1":
                args.route = not args.route
            elif choice == "2":
                def family(value):
                    if value not in ("4", "6", "both"):
                        raise ValueError("请输入 4、6 或 both")
                    return value
                args.family = parsed("IP 版本 4 / 6 / both", args.family, family)
            elif choice == "3":
                args.target = parsed("目标，用逗号分隔", ",".join(args.target or ["one.one.one.one", "dns.google"]),
                                     lambda value: [host(x.strip()) for x in value.split(",")])
            elif choice == "4":
                value = parsed("HTTPS 文件地址，输入 none 关闭", args.download_url or "none",
                               lambda value: None if value.lower() == "none" else https_url(value))
                args.download_url = value
                if value:
                    args.download_mib = parsed("最大下载 MiB（1–256）", args.download_mib, integer_between(1, 256))
            else:
                write("  请输入菜单中的编号。", "warn")

    with tty, output:
        while True:
            show_menu()
            choice = ask("选择", "r").lower()
            if choice in ("r", "start"):
                if not args.modules:
                    feedback.append("请至少选择一项检测。")
                    continue
                if args.png and "Pillow" in missing_tools(args) and not args.install_deps:
                    feedback.append("PNG 需要 Pillow；请选 8 输入 none，或退出后安装依赖。")
                    continue
                write("\n  配置已就绪，开始检测。\n", "accent")
                return
            if choice == "q":
                raise SystemExit(0)
            if choice in ("1", "2", "3", "4"):
                module = MODULES[int(choice) - 1]
                if module in args.modules:
                    args.modules.remove(module)
                    args.stress = [x for x in args.stress if x != module]
                    if module == "disk":
                        args.disk_bench = False
                    if module == "network":
                        args.route, args.download_url = False, None
                else:
                    args.modules.append(module)
            elif choice == "5":
                write("  压力测试：0 关闭 / 1 CPU / 2 内存 / 3 CPU + 内存", "warn")
                stress = parsed("模式", "0", integer_between(0, 3))
                args.stress = {0: [], 1: ["cpu"], 2: ["memory"], 3: ["cpu", "memory"]}[stress]
                args.modules = list(dict.fromkeys(args.modules + args.stress))
            elif choice == "6":
                args.disk_bench = not args.disk_bench
                if args.disk_bench:
                    args.modules = list(dict.fromkeys(args.modules + ["disk"]))
                    def directory(value):
                        if not Path(value).expanduser().is_dir():
                            raise ValueError("目录不存在，请输入实际挂载目录")
                        return value
                    args.disk_dir = parsed("临时测试目录（额外预留 1 GiB）", args.disk_dir, directory)
                    args.disk_mib = parsed("测试文件 MiB（64–16384）", args.disk_mib, integer_between(64, 16384))
            elif choice == "7":
                network_menu()
            elif choice == "8":
                name = str(Path.home() / ("vps-report-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"))
                def png_path(value):
                    if value.lower() == "none":
                        return None
                    path = Path(value).expanduser()
                    if path.exists() or path.is_symlink() or not path.parent.is_dir():
                        raise ValueError("父目录须存在，且不能覆盖现有文件")
                    return str(path)
                args.png = parsed("PNG 路径，输入 none 仅显示文字", args.png or name, png_path)
            elif choice == "9":
                args.duration = parsed("每项测试时长（1–3600 秒）", args.duration, integer_between(1, 3600))
                args.cpu_load = parsed("CPU 目标负载（1–100%）", args.cpu_load, integer_between(1, 100))
                args.memory_mib = parsed("内存压测上限 MiB", args.memory_mib, integer_between(16, 1048576))
                args.workers = parsed("CPU 线程（0 自动）", args.workers or 0, integer_between(0, 256)) or None
            elif choice == "v":
                args.verbose = not args.verbose
            else:
                feedback.append("请输入菜单中的编号，或回车开始。")


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
    global VERBOSE, COLOR, STARTED, SECTION_NUMBER
    p = parser()
    args = p.parse_args(argv)
    LINES.clear()
    ROWS.clear()
    SUMMARY.clear()
    ISSUES.clear()
    SECTION_NUMBER = 0
    STARTED = time.monotonic()
    COLOR = args.color
    if platform.system() != "Linux":
        p.error("Run on a Linux VPS (Python 3.8+), not on the Windows host")
    if args.interactive:
        interactive(args, p)
    VERBOSE = args.verbose
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
    emit("", kind="space")
    for line in frame("VPS INSPECTOR   " + VERSION + "   /   服务器检测报告"):
        emit(line, "accent", "hero")
    metric("Time", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except (AttributeError, OSError):
        os_name = "Linux"
    metric("System", os_name)
    metric("Kernel / arch", platform.release() + " / " + platform.machine())
    metric("Selected", ", ".join(args.modules))
    metric("Output", args.png or "仅终端文字 · 不保存 · 不上传", english=args.png or "Terminal only / not saved / no report upload")
    for item in args.modules:
        try:
            progress("采集 " + item + " 信息…")
            {"cpu": cpu_info, "memory": memory_info, "disk": disk_info,
             "network": lambda: network_info(args)}[item]()
        except (OSError, ValueError) as exc:
            issue(item, "信息采集失败：" + str(exc), "Collection failed: " + str(exc), True)
    if args.stress:
        stress_test(args)
    if args.disk_bench:
        disk_benchmark(args)
    section("SUMMARY")
    for label, value in SUMMARY.items():
        metric(label, value, "accent" if label in ("Sequential write", "Sequential read", "Random write 4K", "Random read 4K", "Download") else None)
    if ISSUES:
        metric("Attention", "{} 项需留意 / 部分测试可能未执行".format(len(ISSUES)), "warn", "{} items need attention; see details below".format(len(ISSUES)))
        for label, text, english in ISSUES:
            note("! {}：{}".format(LABELS.get(label, label), text), label + ": " + english, "warn")
    else:
        note("本次未记录检测失败或跳过；各项指标请结合实际业务判断。", "No failed/skipped checks recorded. Interpret metrics in application context.")
    metric("Elapsed", "{:.1f}s".format(time.monotonic() - STARTED))
    note("报告仅在本次终端显示；选择 PNG 时另存本机，不生成文字文件。", "Report not saved as text. PNG, when requested, stays on this host.")
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
