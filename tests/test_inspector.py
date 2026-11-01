import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build
import inspector as app


class InspectorTests(unittest.TestCase):
    def setUp(self):
        app.LINES.clear()
        app.ROWS.clear()
        app.SUMMARY.clear()
        app.ISSUES.clear()
        app.VERBOSE = False
        app.COLOR = "auto"
        app.SECTION_NUMBER = 0

    def test_built_script_matches_source(self):
        expected = build.HEADER + (ROOT / "inspector.py").read_text(encoding="utf-8") + "\nVPS_INSPECTOR_PYTHON\n"
        self.assertEqual((ROOT / "check.sh").read_bytes(), expected.encode("utf-8"))

    def test_defaults_have_no_load_or_persistence(self):
        args = app.parser().parse_args([])
        self.assertEqual(args.modules, list(app.MODULES))
        self.assertEqual(args.stress, [])
        self.assertFalse(args.disk_bench)
        self.assertFalse(args.route)
        self.assertIsNone(args.download_url)
        self.assertIsNone(args.png)
        self.assertFalse(args.install_deps)

    def test_reject_option_injection_and_bad_limits(self):
        for argv in (["--target=-c100"], ["--target", "x;id"], ["--duration", "0"],
                     ["--download-url", "http://example.com"], ["--modules", "gpu"],
                     ["--http-url", "https://user:pass@example.com"], ["--disk-mib", "999999"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parser().parse_args(argv)

    def test_selected_modules_gate_expensive_tests(self):
        for argv in (["--modules", "disk", "--stress", "cpu"],
                     ["--modules", "cpu", "--disk-bench"], ["--modules", "cpu", "--route"],
                     ["--modules", "cpu", "--download-url", "https://example.com"]):
            parser = app.parser()
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.validate(parser.parse_args(argv), parser)

    def test_cpu_memory_only_never_calls_disk_network_or_stress(self):
        args = ["--modules", "cpu,memory"]
        with patch.object(app.platform, "system", return_value="Linux"), \
             patch.object(app, "cpu_info") as cpu, patch.object(app, "memory_info") as mem, \
             patch.object(app, "disk_info") as disk, patch.object(app, "network_info") as net, \
             patch.object(app, "stress_test") as stress, patch.object(app, "disk_benchmark") as bench, \
             patch.object(app, "save_png") as png, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(args), 0)
            cpu.assert_called_once()
            mem.assert_called_once()
            for mock in (disk, net, stress, bench, png):
                mock.assert_not_called()

    def test_no_terminal_escape_in_report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.say("hello\x1b[31m\rtest\nend")
        self.assertNotIn("\x1b", "\n".join(app.LINES))
        self.assertNotIn("\r", "\n".join(app.LINES))

    def test_cjk_alignment_and_terminal_color_policy(self):
        self.assertEqual(app.cells(app.pad("内存", 16)), 16)
        self.assertLessEqual(app.cells(app.fit("中文标签abcdefghijk", 10)), 10)
        self.assertEqual(len(set(app.cells(line) for line in app.frame("中文报告"))), 1)
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}):
            self.assertEqual(app.colored("test", "ok", Terminal()), "test")
        app.COLOR = "always"
        self.assertIn("\x1b[", app.colored("test", "ok", Terminal()))
        app.COLOR = "never"
        self.assertNotIn("\x1b", app.colored("test", "ok", Terminal()))

    def test_number_menu_retries_and_configures_without_flag_syntax(self):
        class Stream(io.StringIO):
            def close(self):
                pass
        # Invalid key, CPU-only selection, CPU stress, short low-load settings,
        # verbose toggle, then run. No real probes or load are executed here.
        source = Stream("x\n2\n3\n4\n5\n1\n9\n1\n10\n16\n1\nv\nr\n")
        output = Stream()
        args = app.parser().parse_args([])
        def tty_open(path, mode, **kwargs):
            self.assertEqual(path, "/dev/tty")
            return source if mode == "r" else output
        with patch.object(app, "open", side_effect=tty_open, create=True), \
             patch.object(app, "missing_tools", return_value=[]):
            app.interactive(args, app.parser())
        self.assertEqual(args.modules, ["cpu"])
        self.assertEqual(args.stress, ["cpu"])
        self.assertEqual((args.duration, args.cpu_load, args.memory_mib, args.workers), (1, 10, 16, 1))
        self.assertTrue(args.verbose)
        self.assertIn("请输入菜单中的编号", output.getvalue())

    def test_compact_ping_metrics_hide_raw_packets_until_verbose(self):
        text = "64 bytes from 1.1.1.1: icmp_seq=1 time=0.811 ms\n5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 0.811/0.869/0.947/0.055 ms"
        args = app.parser().parse_args(["--modules", "network", "-4", "--target", "1.1.1.1"])
        def which(name):
            return name if name == "ping" else None
        with patch.object(app.shutil, "which", side_effect=which), \
             patch.object(app, "run", return_value=(0, text)), contextlib.redirect_stdout(io.StringIO()):
            app.network_info(args)
        report = "\n".join(app.LINES)
        self.assertIn("AVG 0.87 ms", report)
        self.assertIn("LOSS 0%", report)
        self.assertNotIn("icmp_seq", report)
        app.VERBOSE = True
        with contextlib.redirect_stdout(io.StringIO()):
            app.raw_output(text)
        self.assertIn("icmp_seq", "\n".join(app.LINES))

    def test_missing_stress_is_retained_in_final_attention_summary(self):
        with patch.object(app.platform, "system", return_value="Linux"), \
             patch.object(app, "cpu_info"), patch.object(app.shutil, "which", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            app.main(["--modules", "cpu", "--stress", "cpu"])
        self.assertTrue(app.ISSUES)
        self.assertIn("缺少 stress-ng", "\n".join(app.LINES))
        self.assertIn("需要注意", "\n".join(app.LINES))

    def test_download_cap_when_server_ignores_range(self):
        args = app.parser().parse_args(["--download-url", "https://example.com/file", "--download-mib", "1"])
        class FakeProcess:
            stdout = io.BytesIO(b"x" * (2 * 1048576))
            def wait(self, timeout=None):
                return 0
        proc = FakeProcess()
        with patch.object(app.shutil, "which", return_value="curl"), \
             patch.object(app.subprocess, "Popen", return_value=proc), \
             patch.object(app, "stop_process") as stop, contextlib.redirect_stdout(io.StringIO()):
            app.download_test(args)
        self.assertTrue(proc.stdout.closed)
        self.assertIn("Received 1.00 MiB", "\n".join(app.LINES))
        self.assertEqual(stop.call_count, 2)

    def test_cgroup_memory_ceiling(self):
        def group(name):
            return {"memory.max": [str(512 * 1048576)], "memory.current": [str(384 * 1048576)]}.get(name, [])
        with patch.object(app, "meminfo", return_value={"MemAvailable": 8 * 1024**3}), \
             patch.object(app, "cgroup_value", side_effect=group), patch.object(app, "read", return_value=""):
            self.assertEqual(app.available_memory(), 128 * 1048576)

    def test_memory_stress_reserves_headroom_and_caps_budget(self):
        args = app.parser().parse_args(["--stress", "memory", "--memory-mib", "4096"])
        with patch.object(app.shutil, "which", return_value="stress-ng"), \
             patch.object(app, "available_memory", return_value=1024 * 1048576), \
             patch.object(app, "show_command", return_value=0) as command, \
             contextlib.redirect_stdout(io.StringIO()):
            app.stress_test(args)
        cmd = command.call_args.args[0]
        self.assertEqual(int(cmd[cmd.index("--vm-bytes") + 1]), 256 * 1048576)

    def test_low_memory_skips_stress(self):
        args = app.parser().parse_args(["--stress", "memory"])
        with patch.object(app.shutil, "which", return_value="stress-ng"), \
             patch.object(app, "available_memory", return_value=128 * 1048576), \
             patch.object(app, "show_command") as command, contextlib.redirect_stdout(io.StringIO()):
            app.stress_test(args)
        command.assert_not_called()

    def test_disk_file_cleanup_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parser().parse_args(["--disk-bench", "--disk-dir", directory, "--duration", "1"])
            calls = []
            def fake_run(cmd, timeout=15):
                calls.append(cmd)
                if cmd == ["fio", "--version"]:
                    return 0, "fio-3.36"
                filename = next(x.split("=", 1)[1] for x in cmd if x.startswith("--filename="))
                Path(filename).write_bytes(b"test")
                raise KeyboardInterrupt
            with patch.object(app.shutil, "which", return_value="fio"), \
                 patch.object(app.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10 * 1024**3})()), \
                 patch.object(app, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(KeyboardInterrupt):
                app.disk_benchmark(args)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertIn("--direct=1", calls[-1])

    def test_disk_low_space_does_not_start_fio_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parser().parse_args(["--disk-bench", "--disk-dir", directory])
            with patch.object(app.shutil, "which", return_value="fio"), \
                 patch.object(app.shutil, "disk_usage", return_value=type("Disk", (), {"free": 32 * 1048576})()), \
                 patch.object(app, "run", return_value=(0, "fio-3.36")) as command, \
                 contextlib.redirect_stdout(io.StringIO()):
                app.disk_benchmark(args)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_png_no_overwrite_and_valid_image(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            filename = Path(directory) / "report.png"
            app.say("VPS Inspector TEST\nCPU: example\nMemory: 1024 MiB")
            app.save_png(filename)
            original = filename.read_bytes()
            with Image.open(filename) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.width, 1000)
            with self.assertRaises(FileExistsError):
                app.save_png(filename)
            self.assertEqual(filename.read_bytes(), original)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["report.png"])

    @unittest.skipUnless(os.name == "posix", "Linux process groups required")
    def test_interactive_standalone_heredoc_uses_controlling_tty(self):
        import pty
        import select
        import time
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("bash", ["bash", str(ROOT / "check.sh"), "-N"])
        data = bytearray()
        status = None
        try:
            os.write(fd, b"2\n3\n4\nr\n")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], .2)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data.extend(chunk)
                done, value = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = value
                    break
            if status is None:
                done, value = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = value
            if status is None:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
            text = data.decode(errors="replace")
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, text)
            self.assertIn("检测项目", text)
            self.assertIn("cpu", text)
            self.assertIn("报告仅在本次终端", text)
        finally:
            os.close(fd)

    @unittest.skipUnless(os.name == "posix", "Linux process groups required")
    def test_timeout_terminates_children(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            script = "import subprocess,time,pathlib; p=subprocess.Popen(['sleep','30']); pathlib.Path({!r}).write_text(str(p.pid)); time.sleep(30)".format(str(pidfile))
            code, out = app.run([sys.executable, "-c", script], timeout=1)
            self.assertEqual(code, 124)
            self.assertIn("TIMEOUT", out)
            pid = int(pidfile.read_text())
            # A dead child can remain a zombie briefly until init reaps it.
            status = Path("/proc/{}/status".format(pid))
            if status.exists():
                self.assertIn("Z (zombie)", status.read_text())


if __name__ == "__main__":
    unittest.main()
