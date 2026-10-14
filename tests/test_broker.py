"""Device-free protocol and security integration tests for the native broker."""
import os
import pathlib
import pty
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import zipapp
import fcntl
import hashlib

REPO = pathlib.Path(__file__).resolve().parents[1]
BASE = b"112233,445566,778899,AABBCC"
ALT = b"A1B2C3,001122,FFEEDD,ABCDEF"


class BrokerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="broker-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = pathlib.Path(cls.tmp.name) / "device"
        cls.root.mkdir()
        cls.sockpath = pathlib.Path(cls.tmp.name) / "broker.sock"
        (REPO / "build").mkdir(exist_ok=True)
        cls.bin_tmp = tempfile.TemporaryDirectory(prefix="broker-bin-", dir=REPO / "build")
        cls.addClassCleanup(cls.bin_tmp.cleanup)
        cls.binary = pathlib.Path(cls.bin_tmp.name) / "broker"
        args = ["cc", "-std=c11", "-D_GNU_SOURCE", "-O2", "-Wall", "-Wextra", "-Werror",
                "-DBROKER_TESTING", f'-DBROKER_TEST_ROOT="{cls.root}"',
                f'-DBROKER_TEST_SOCKET="{cls.sockpath}"',
                str(REPO / "broker" / "main.c"), "-o", str(cls.binary)]
        if os.environ.get("BROKER_SANITIZE") == "1":
            args[1:1] = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g"]
        subprocess.run(args, check=True, cwd=REPO)
        # Interpose only in the fake-device process: emulate kernel changes that
        # regular test files cannot perform synchronously after a sysfs write.
        shim = pathlib.Path(cls.bin_tmp.name) / "fake_kernel.c"
        cls.fake_kernel = pathlib.Path(cls.bin_tmp.name) / "fake_kernel.so"
        shim.write_text(r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#ifndef BROKER_TEST_ROOT
#error "fake kernel must be built for the fake root"
#endif
static void trace_wmi(char kind) {
    const char *log = getenv("BROKER_WMI_TRACE");
    if (!log) return;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return;
    int fd = open(log, O_WRONLY | O_APPEND | O_CREAT, 0600);
    if (fd < 0) return;
    char line[64];
    int n = snprintf(line, sizeof(line), "%lld %c\n",
                     (long long)now.tv_sec * 1000000000LL + now.tv_nsec, kind);
    syscall(SYS_write, fd, line, n);
    close(fd);
}
static char attribute_kind(int fd) {
    char link[64], path[512];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t size = readlink(link, path, sizeof(path) - 1);
    if (size < 0) return 0;
    path[size] = 0;
    if (!strcmp(path, BROKER_TEST_ROOT "/colors")) return 'C';
    if (!strcmp(path, BROKER_TEST_ROOT "/state")) return 'S';
    return 0;
}
ssize_t read(int fd, void *buf, size_t len) {
    ssize_t n = syscall(SYS_read, fd, buf, len);
    char kind = attribute_kind(fd);
    if (n > 0 && kind) trace_wmi(kind); /* timestamp the actual read */
    const char *log = getenv("BROKER_WMI_TRACE");
    if (n > 0 && kind == 'S' && log) {
        char marker[1024];
        int size = snprintf(marker, sizeof(marker), "%s.slow", log);
        if (size > 0 && size < (int)sizeof(marker) && access(marker, F_OK) == 0) {
            struct timespec delay = { .tv_sec = 1, .tv_nsec = 50000000 };
            nanosleep(&delay, NULL);
        } else if (size > 0 && size < (int)sizeof(marker) &&
                   snprintf(marker, sizeof(marker), "%s.slow-once", log) > 0 &&
                   access(marker, F_OK) == 0) {
            unlink(marker); /* exactly one delayed state read, then recover */
            struct timespec delay = { .tv_sec = 1, .tv_nsec = 50000000 };
            nanosleep(&delay, NULL);
        }
    }
    return n;
}
ssize_t write(int fd, const void *buf, size_t len) {
    ssize_t n = syscall(SYS_write, fd, buf, len);
    const char *mode = getenv("BROKER_FAKE_KERNEL");
    if (!mode || n != (ssize_t)len) return n;
    char link[64], path[512];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t size = readlink(link, path, sizeof(path) - 1);
    if (size < 0) return n;
    path[size] = 0;
    if ((!strcmp(path, BROKER_TEST_ROOT "/colors") ||
         !strcmp(path, BROKER_TEST_ROOT "/restore")) && n == (ssize_t)len) {
        trace_wmi('I'); /* kernel colors_store/restore_store pre-read */
        trace_wmi('I'); /* kernel post-write verification read */
    }
    if (!strcmp(mode, "restore") && !strcmp(path, BROKER_TEST_ROOT "/restore")) {
        int source = open(BROKER_TEST_ROOT "/original", O_RDONLY | O_NOFOLLOW);
        int target = open(BROKER_TEST_ROOT "/colors", O_WRONLY | O_NOFOLLOW);
        char value[128];
        ssize_t read_size = source < 0 ? -1 : read(source, value, sizeof(value));
        if (target >= 0 && read_size > 0) {
            syscall(SYS_write, target, value, read_size);
            ftruncate(target, read_size);
        }
        if (source >= 0) close(source);
        if (target >= 0) close(target);
    } else if (!strcmp(path, BROKER_TEST_ROOT "/colors") &&
               (!strcmp(mode, "mismatch") || !strcmp(mode, "malformed"))) {
        const char *value = !strcmp(mode, "mismatch") ?
            "112233,445566,778899,AABBCC\n" : "bad\n";
        lseek(fd, 0, SEEK_SET);
        syscall(SYS_write, fd, value, strlen(value));
        ftruncate(fd, strlen(value));
    }
    return n;
}
''')
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                        f'-DBROKER_TEST_ROOT="{cls.root}"', str(shim), "-o",
                        str(cls.fake_kernel)], check=True, cwd=REPO)

    def setUp(self):
        self.root.chmod(0o755)
        (self.root / "rate-experiment-used").unlink(missing_ok=True)
        (self.root / "rate-experiment-2-used").unlink(missing_ok=True)
        (self.root / "rate-experiment-3-used").unlink(missing_ok=True)
        (self.root / "normal-bounded-used").unlink(missing_ok=True)
        (self.root / "long-4hz-diagnostic-used").unlink(missing_ok=True)
        for name in ("abi_version", "colors", "original", "state", "restore"):
            path = self.root / name
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.put("abi_version", b"1\n")
        self.put("colors", BASE + b"\n")
        self.put("original", BASE + b"\n")
        self.put("state", b"on\n")
        self.put("restore", b"")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.sockpath))
        os.chmod(self.sockpath, 0o660)
        self.listener.listen(16)
        self.launch_broker()
        self.addCleanup(self.stop)

    def launch_broker(self, fake_mode=None, trace=None):
        fd = self.listener.fileno()
        command = f"exec 3<&{fd}; LISTEN_PID=$$ exec {shlex.quote(str(self.binary))}"
        env = dict(os.environ, LISTEN_FDS="1")
        if trace is not None:
            env["BROKER_WMI_TRACE"] = str(trace)
        if fake_mode is not None:
            preload = str(self.fake_kernel)
            if os.environ.get("BROKER_SANITIZE") == "1":
                asan = subprocess.check_output(["cc", "-print-file-name=libasan.so"],
                                               text=True).strip()
                preload = asan + ":" + preload
            env.update(BROKER_FAKE_KERNEL=fake_mode, LD_PRELOAD=preload)
        self.proc = subprocess.Popen(["/bin/sh", "-c", command], pass_fds=(fd,),
                                     env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        # No fixed sleeps: wait until the process has inherited its listener.
        self.assertIsNone(self.proc.poll(), self.proc.stderr.read() if self.proc.poll() is not None else "")

    def restart_with_fake_kernel(self, mode, trace=None):
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker(mode, trace=trace)

    def wmi_trace(self):
        trace = pathlib.Path(self.tmp.name) / "wmi-trace"
        trace.write_text("")
        self.restart_with_fake_kernel("trace", trace=trace)
        return trace

    def observed_wmi(self, trace):
        return [(int(timestamp), kind) for timestamp, kind in
                (line.split() for line in trace.read_text().splitlines())]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate(timeout=2)
        self.listener.close()
        self.sockpath.unlink(missing_ok=True)

    def put(self, name, data):
        path = self.root / name
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(data)
        path.chmod({"abi_version": 0o444, "colors": 0o600,
                    "original": 0o444, "state": 0o400,
                    "restore": 0o200}[name])

    def request(self, data, second=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(3)
            client.connect(str(self.sockpath))
            client.sendall(data)
            if second is not None:
                client.sendall(second)
            return client.recv(256)

    def snapshot(self):
        response = self.request(b"2 SNAPSHOT\n")
        match = re.fullmatch(rb"OK ([0-9A-F]{16}:[0-9A-F]{16}) (on|off) "
                             rb"([0-9A-F]{6},[0-9A-F]{6},[0-9A-F]{6},[0-9A-F]{6}) "
                             rb"([0-9A-F]{6},[0-9A-F]{6},[0-9A-F]{6},[0-9A-F]{6})\n", response)
        self.assertIsNotNone(match, response)
        return match.groups()

    def cas(self, token, state=b"on", expected=BASE, desired=ALT):
        return self.request(b"2 CAS " + token + b" " + state + b" " + expected +
                            b" " + desired + b"\n")

    def test_snapshot_and_cas_return_new_token_after_verified_write(self):
        token, state, colors, original = self.snapshot()
        self.assertEqual((state, colors, original), (b"on", BASE, BASE))
        reply = self.cas(token)
        self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
        new_token = reply[3:-1]
        self.assertNotEqual(token, new_token)
        self.assertEqual(new_token[:16], token[:16])
        self.assertEqual(int(new_token[17:], 16), int(token[17:], 16) + 1)
        self.assertEqual(self.snapshot(), (new_token, b"on", ALT, BASE))

    def test_competing_clients_cannot_overwrite_a_stale_snapshot(self):
        stale = self.snapshot()[0]
        fresh = self.snapshot()[0]
        self.assertEqual(stale, fresh)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as first, \
             socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as second:
            for client in (first, second):
                client.settimeout(3)
                client.connect(str(self.sockpath))
            first.sendall(b"2 CAS " + fresh + b" on " + BASE + b" " + ALT + b"\n")
            second.sendall(b"2 CAS " + stale + b" on " + BASE + b" " + BASE + b"\n")
            self.assertRegex(first.recv(256), rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            self.assertEqual(second.recv(256), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")

    def test_aba_colors_do_not_resurrect_old_token(self):
        stale = self.snapshot()[0]
        newer = self.cas(stale)[3:-1]
        self.assertTrue(self.cas(newer, expected=ALT, desired=BASE).startswith(b"OK "))
        self.assertEqual(self.snapshot()[2], BASE)
        self.assertEqual(self.cas(stale), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_restart_invalidates_token_even_without_mutation(self):
        stale = self.snapshot()[0]
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        fresh = self.snapshot()[0]
        self.assertNotEqual(stale, fresh)
        self.assertEqual(self.cas(stale), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_legacy_and_uncertain_mutations_invalidate_token(self):
        stale = self.snapshot()[0]
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        self.assertEqual(self.cas(stale), b"ERR CONFLICT\n")
        token = self.snapshot()[0]
        self.assertEqual(self.request(b"1 RESTORE\n"), b"ERR IO\n")
        self.assertEqual(self.cas(token, expected=ALT), b"ERR CONFLICT\n")
        current = self.snapshot()[0]
        self.assertEqual(int(current[17:], 16), int(token[17:], 16) + 1)

    def test_cas_checks_power_and_colors_without_writing_on_conflict(self):
        token = self.snapshot()[0]
        self.put("state", b"off\n")
        self.assertEqual(self.cas(token), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.put("state", b"on\n")
        self.put("colors", ALT + b"\n")
        self.assertEqual(self.cas(token), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        self.assertEqual(self.snapshot()[0], token)

    def test_set_refuses_power_off_without_write_or_token_invalidation(self):
        token = self.snapshot()[0]
        self.put("state", b"off\n")

        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual(self.snapshot()[0], token)

    def test_restore_refuses_power_off_without_touching_restore_attribute(self):
        self.put("state", b"off\n")
        self.assertEqual(self.request(b"1 RESTORE\n"), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "restore").read_bytes(), b"")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_malformed_version_two_requests_never_write(self):
        token = b"A" * 16 + b":" + b"0" * 16
        valid = b"2 CAS " + token + b" on " + BASE + b" " + ALT + b"\n"
        packets = [b"2 SNAPSHOT", b"2 SNAPSHOT \n", b"3 SNAPSHOT\n",
                   valid.replace(token, token.lower()), valid.replace(token, token.replace(b":", b"-")),
                   valid.replace(b" on ", b" ON "), valid.replace(BASE, BASE.lower()),
                   valid + b"extra", valid + b"X" * 128]
        for packet in packets:
            with self.subTest(packet=packet):
                self.assertEqual(self.request(packet), b"ERR INVALID\n")
                self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_snapshot_cas_reserves_all_seven_firmware_reads(self):
        trace = self.wmi_trace()
        token = self.snapshot()[0]
        self.assertTrue(self.cas(token).startswith(b"OK "))
        self.assertEqual([kind for _, kind in self.observed_wmi(trace)],
                         ["C", "S", "S", "C", "I", "I", "C"])
        for _ in range(2):
            token = self.snapshot()[0]
            current = (self.root / "colors").read_bytes().strip()
            desired = BASE if current == ALT else ALT
            self.assertTrue(self.cas(token, expected=current, desired=desired).startswith(b"OK "))
        token = self.snapshot()[0]
        current = (self.root / "colors").read_bytes().strip()
        desired = BASE if current == ALT else ALT
        reply = self.cas(token, expected=current, desired=desired)
        self.assertRegex(reply, rb"\A(?:OK [0-9A-F]{16}:[0-9A-F]{16}|ERR BUSY)\n\Z")
        events = self.observed_wmi(trace)
        if reply.startswith(b"OK "):
            self.assertEqual(len(events), 28)
        else:
            # Deadline may expire after admission and one or both CAS checks.
            self.assertGreaterEqual(len(events), 23)
            self.assertLessEqual(len(events), 25)
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 25, events)

    def test_competing_status_cannot_spend_cas_kernel_and_readback_reserve(self):
        trace = self.wmi_trace()
        token = self.snapshot()[0]
        for _ in range(4):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")
        self.assertTrue(self.cas(token).startswith(b"OK "))
        self.assertEqual(len(self.observed_wmi(trace)), 15)

    def test_stop_waits_for_status_capacity_then_conditionally_restores(self):
        trace = self.wmi_trace()
        token = self.snapshot()[0]
        self.assertTrue(self.cas(token).startswith(b"OK "))
        self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        time.sleep(0.3)
        for _ in range(3):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        began = time.monotonic()
        response = self.request(b"2 STOPSHOT\n")
        self.assertTrue(response.startswith(b"OK "), response)
        self.assertGreaterEqual(time.monotonic() - began, 0.55)
        stop_token = response.split(b" ")[1]
        self.assertTrue(self.cas(stop_token, expected=ALT, desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(len(events), 22)  # first CAS cycle, four STATUS, Stop
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 25)

    def test_two_hz_snapshots_and_cas_sustain_verified_updates(self):
        start = time.monotonic()
        expected, desired = BASE, ALT
        for step in range(6):
            target = start + step * 0.51
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            self.assertRegex(self.cas(token, expected=expected, desired=desired),
                             rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            expected, desired = desired, expected
        self.assertEqual((self.root / "colors").read_bytes(), expected + b"\n")

    def test_ordinary_sustained_four_and_five_hz_hit_rolling_read_ceiling(self):
        # A kernel-facing trace includes two hidden store GETs per CAS, not
        # merely the broker's state/colors/readback sysfs reads.
        for rate in (4, 5):
            with self.subTest(rate=rate):
                self.put("colors", BASE + b"\n")
                trace = self.wmi_trace()
                expected, desired = BASE, ALT
                lateness = []
                started = time.monotonic()
                for index in range(6):
                    due = started + index / rate
                    if due > time.monotonic():
                        time.sleep(due - time.monotonic())
                    token, state, colors, _ = self.snapshot()
                    self.assertEqual((state, colors), (b"on", expected))
                    reply = self.cas(token, expected=expected, desired=desired)
                    lateness.append(time.monotonic() - due)
                    self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
                    expected, desired = desired, expected
                # Deadline-to-ack lateness includes waits in either SNAPSHOT
                # or CAS: four seven-GET frames cannot fit within a second.
                self.assertGreater(lateness[3], 0.12, lateness)
                stop = self.request(b"2 STOPSHOT\n")
                self.assertTrue(stop.startswith(b"OK "), stop)
                self.assertEqual(stop.split(b" ")[3], expected)
                self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                         desired=BASE).startswith(b"OK "))
                events = self.observed_wmi(trace)
                self.assertEqual(len(events), 6 * 7 + 2 + 5)
                self.assertEqual([kind for _, kind in events[:7]],
                                 ["C", "S", "S", "C", "I", "I", "C"])
                self.assertEqual([kind for _, kind in events[-7:]],
                                 ["C", "S", "S", "C", "I", "I", "C"])
                for at, _ in events:
                    self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                             for when, _ in events), 25, events)
                self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_candidate_sustains_four_and_five_hz_with_stop_headroom(self):
        # A test-only candidate; the installed/default broker stays ordinary.
        candidate = pathlib.Path(self.bin_tmp.name) / "candidate-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_SUSTAINED_CANDIDATE",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(candidate)], check=True)
        for rate in (4, 5):
            with self.subTest(rate=rate):
                self.proc.terminate()
                self.proc.communicate(timeout=2)
                self.put("colors", BASE + b"\n")
                trace = pathlib.Path(self.tmp.name) / f"candidate-{rate}-wmi"
                trace.write_text("")
                self.binary = candidate
                self.launch_broker("trace", trace=trace)
                expected, desired = BASE, ALT
                started = time.monotonic()
                lateness = []
                extra_statuses = []
                for index in range(rate * 3):
                    due = started + index / rate
                    if due > time.monotonic():
                        time.sleep(due - time.monotonic())
                    token, state, colors, _ = self.snapshot()
                    self.assertEqual((state, colors), (b"on", expected))
                    if index == rate:
                        for _ in range(2):
                            reply = self.request(b"1 STATUS\n")
                            self.assertTrue(reply.startswith(b"OK on ") or
                                            reply == b"ERR BUSY\n", reply)
                            extra_statuses.append(reply)
                    reply = self.cas(token, expected=expected, desired=desired)
                    self.assertTrue(reply.startswith(b"OK "), reply)
                    lateness.append(time.monotonic() - due)
                    expected, desired = desired, expected
                self.assertLess(max(lateness), 0.18, lateness)
                began = time.monotonic()
                stop = self.request(b"2 STOPSHOT\n")
                self.assertTrue(stop.startswith(b"OK "), stop)
                self.assertLess(time.monotonic() - began, 0.18)
                self.assertEqual(stop.split(b" ")[3], expected)
                self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                         desired=BASE).startswith(b"OK "))
                self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
                events = self.observed_wmi(trace)
                self.assertEqual(len(events), rate * 3 * 7 +
                                 2 * sum(reply.startswith(b"OK ") for reply in extra_statuses) + 7)
                for at, _ in events:
                    window = [kind for when, kind in events
                              if at <= when < at + 1_000_000_000]
                    self.assertLessEqual(len(window), 49, events)
                    self.assertLessEqual(window.count("I"), 12, events)
        self.binary = self.__class__.binary

    def test_normal_build_opt_in_lease_admits_five_hz_with_owned_stop(self):
        # Ordinary build policy in a fake-device compilation, not a campaign.
        source = (REPO / "Makefile").read_text()
        rule = source.split("build/okeylitctl-broker: broker/main.c")[1].split("\n\n")[0]
        self.assertIn("-DBROKER_BOUNDED_NORMAL", rule)
        normal = pathlib.Path(self.bin_tmp.name) / "bounded-normal-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / "bounded-normal-wmi"
        trace.write_text("")
        legacy = self.root / "rate-experiment-used"
        legacy.write_bytes(b"consumed earlier diagnostic")
        self.launch_broker("trace", trace=trace)
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"OK BURST\n")
        self.assertEqual(legacy.read_bytes(), b"consumed earlier diagnostic")
        self.assertTrue((self.root / "normal-bounded-used").exists())
        expected, desired = BASE, ALT
        started = time.monotonic()
        for index in range(5):
            due = started + index * 0.2
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            reply = self.cas(token, expected=expected, desired=desired)
            self.assertTrue(reply.startswith(b"OK "), reply)
            expected, desired = desired, expected
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR BUSY\n")
        # A 1.05s blocked firmware read still defeats the one-second request
        # deadline. Preserve the frame; a later owned Stop may restore it.
        remaining = started + 1.1 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.assertTrue(self.cas(self.snapshot()[0], expected=BASE,
                                 desired=ALT).startswith(b"OK "))
        slow = pathlib.Path(str(trace) + ".slow")
        slow.touch()
        try:
            self.assertEqual(self.request(b"2 STOPSHOT\n"), b"ERR BUSY\n")
        finally:
            slow.unlink(missing_ok=True)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=ALT,
                                 desired=BASE).startswith(b"OK "))
        remaining = started + 3.1 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        time.sleep(1.1)  # all lease-era reservations have left the window
        for _ in range(5):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR BUSY\n")
        self.assertEqual(legacy.read_bytes(), b"consumed earlier diagnostic")
        events = self.observed_wmi(trace)
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 49, events)

    def test_normal_build_four_hz_uses_same_lease_and_priority_stop(self):
        normal = pathlib.Path(self.bin_tmp.name) / "bounded-normal-4"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / "bounded-normal-4-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)
        self.assertEqual(self.request(b"2 BURST 4\n"), b"OK BURST\n")
        started = time.monotonic()
        expected, desired = BASE, ALT
        for index in range(4):
            due = started + index * 0.25
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            self.assertTrue(self.cas(token, expected=expected,
                                     desired=desired).startswith(b"OK "))
            expected, desired = desired, expected
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertEqual(stop.split(b" ")[3], expected)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 49, events)
        remaining = started + 3.1 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        time.sleep(1.1)  # former lease reservations must leave rolling window
        for _ in range(5):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR BUSY\n")

    def test_normal_cli_tui_start_tick_visible_stop_uses_ipc_and_restores(self):
        import io
        from unittest import mock
        from okeylitctl.cli import main
        from okeylitctl.ipc import IPCBackend
        from test_tui import FakeProfileStore

        normal = pathlib.Path(self.bin_tmp.name) / "normal-tui-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / "normal-tui-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)

        class Screen:
            def __init__(self):
                self.keys = iter((9, -1, ord("s"), ord("q")))
                self.drawn = []

            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, _y, _x, text, _length, *_attr):
                self.drawn.append(text)
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == -1:
                    time.sleep(0.28)
                return key

        screen = Screen()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("okeylitctl.cli.IPCBackend",
                        side_effect=lambda: IPCBackend(socket_path=self.sockpath)), \
             mock.patch("okeylitctl.tui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            code = main(["tui", "--test-rate-hz", "5", "--rate-experiment"],
                        profile_store=FakeProfileStore(), stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(any("S Stop" in text for text in screen.drawn), screen.drawn[-20:])
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n",
                         [text for text in screen.drawn if any(word in text.lower() for word in
                          ("stop", "restore", "unknown", "ownership", "pending", "effect state", "broker"))][-25:])
        events = self.observed_wmi(trace)
        self.assertGreaterEqual(sum(kind == "I" for _, kind in events), 2)
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 49, events)

    def _sweep_normal_tui_effect(self, effect, rate):
        """Each test's setUp provides a fresh fake device/broker lease."""
        import io
        from unittest import mock
        from okeylitctl.cli import main
        from okeylitctl.effects import EffectKind
        from okeylitctl.ipc import IPCBackend
        from okeylitctl.tui import CursesTui
        from test_tui import FakeProfileStore

        normal = pathlib.Path(self.bin_tmp.name) / "normal-effect-sweep"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / f"sweep-{effect.value}-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)

        class Screen:
            def __init__(self):
                # Tab enters the same input router as the physical TUI. The
                # event effects receive actual foreground input, not an event
                # injected into the frame generator.
                keys = [9]
                if effect in (EffectKind.REACTIVE, EffectKind.RIPPLE):
                    keys.append(ord("z"))
                keys.extend([-1] * 6)
                keys.extend((ord("s"), ord("q")))
                self.keys = iter(keys)
                self.drawn = []
                self.stops = []
                self.live_frames = []

            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, _y, _x, text, _length, *_attr):
                self.drawn.append(text)
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == -1:
                    self.live_frames.append((root / "colors").read_bytes())
                    time.sleep(0.22 if rate == 5 else 0.28)
                return key

        root = self.root
        screen = Screen()
        load_state = CursesTui._load_state
        stop_effect = CursesTui._stop_effect
        exchanges = []

        class TracedIPCBackend(IPCBackend):
            def _exchange(self, request, *, mutation):
                response = super()._exchange(request, mutation=mutation)
                exchanges.append((request, response))
                return response

        def selected_state(app):
            state = load_state(app)
            state.effect_index = list(EffectKind).index(effect) - 1
            return state

        def observed_stop(app):
            result = stop_effect(app)
            screen.stops.append((result, app.effect_runtime is None,
                                 app.state.effect_restore_pending,
                                 (self.root / "colors").read_bytes()))
            return result

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("okeylitctl.cli.IPCBackend",
                        side_effect=lambda: TracedIPCBackend(socket_path=self.sockpath)), \
             mock.patch("okeylitctl.tui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False), \
             mock.patch.object(CursesTui, "_load_state", selected_state), \
             mock.patch.object(CursesTui, "_stop_effect", observed_stop):
            code = main(["tui", "--test-rate-hz", str(rate), "--rate-experiment"],
                        profile_store=FakeProfileStore(), stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(screen.stops, (effect, screen.drawn[-20:]))
        self.assertEqual(screen.stops[0], (True, True, False, BASE + b"\n"),
                         (effect, screen.stops, screen.drawn[-20:]))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertTrue((self.root / "normal-bounded-used").exists())
        self.assertTrue(any(frame != BASE + b"\n" for frame in screen.live_frames),
                        (effect, screen.live_frames))
        self.assertGreater(len(set(screen.live_frames)), 1,
                           (effect, screen.live_frames,
                            self.observed_wmi(trace), screen.drawn[-30:]))
        events = self.observed_wmi(trace)
        writes = sum(kind == "I" for _, kind in events)
        self.assertGreater(writes, 0, (effect, events))
        self.assertEqual(writes % 2, 0, (effect, events))
        self.assertLessEqual(writes, 22, (effect, events))  # ten frames + Stop
        mutations = [request for request, _ in exchanges
                     if request.startswith((b"2 CAS ", b"1 SET ", b"1 RESTORE"))]
        self.assertEqual(len(mutations), writes // 2, (effect, exchanges))
        self.assertTrue(all(request.startswith(b"2 CAS ") for request in mutations),
                        (effect, exchanges))
        snapshot = None
        for request, response in exchanges:
            if request in (b"2 SNAPSHOT\n", b"2 STOPSHOT\n") and response.startswith(b"OK "):
                snapshot = response.split()
            if request.startswith(b"2 CAS "):
                parts = request.split()
                self.assertIsNotNone(snapshot, (effect, exchanges))
                self.assertEqual(parts[2], snapshot[1], (effect, exchanges))
                self.assertEqual(parts[4], snapshot[3], (effect, exchanges))
                self.assertTrue(response.startswith(b"OK "), (effect, exchanges))
                snapshot = None
        for at, _ in events:
            self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                     for when, _ in events), 49, (effect, events))

    def _pty_frontend(self, rate, *, packaged, fake_mode="trace", size=(30, 110),
                      installed_broker=None, installed_archive=None, test_effect=None,
                      long_session=False):
        """Run real curses/entrypoint; only redirect its default broker socket."""
        normal = installed_broker or pathlib.Path(self.bin_tmp.name) / (
            "long-pty-broker" if long_session else "normal-pty-broker")
        if installed_broker is None:
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-DBROKER_TESTING", ("-DBROKER_LONG_4HZ_DIAGNOSTIC"
                                                 if long_session else "-DBROKER_BOUNDED_NORMAL"),
                            f'-DBROKER_TEST_ROOT="{self.root}"',
                            f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                            str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / f"pty-{rate}-{packaged}-{fake_mode}.trace"
        trace.write_text("")
        self.launch_broker(fake_mode, trace=trace)

        hook = pathlib.Path(self.tmp.name) / "sitecustomize.py"
        hook.write_text("from pathlib import Path\n"
                        "from okeylitctl.ipc import IPCBackend\n"
                        f"IPCBackend.__init__.__kwdefaults__['socket_path'] = Path({str(self.sockpath)!r})\n")
        if packaged:
            archive = installed_archive or pathlib.Path(self.tmp.name) / "okeylitctl.pyz"
            if installed_archive is None:
                zipapp.create_archive(REPO / "src", target=archive,
                                      main="okeylitctl.cli:entrypoint")
            command = [sys.executable, str(archive), "tui", "--test-rate-hz",
                       str(rate), "--rate-experiment"]
            if test_effect is not None:
                command.extend(("--test-effect", test_effect))
            if long_session:
                command.extend(("--test-duration-seconds", "12"))
            package_path = archive
        else:
            command = [sys.executable, "-c",
                       "from okeylitctl.tui import run_tui; "
                       f"run_tui(test_rate_hz={rate}, rate_experiment=True)"]
            package_path = REPO / "src"
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", XDG_DATA_HOME=str(self.root),
                   PYTHONPATH=os.pathsep.join((str(self.tmp.name), str(package_path))))
        child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                 env=env, start_new_session=True,
                                 preexec_fn=lambda: fcntl.ioctl(slave, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.addCleanup(lambda: self._close_pty(child, master))
        return child, master, trace

    @staticmethod
    def _close_pty(child, master):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        os.close(master)

    def _pty_until(self, child, master, predicate, *, timeout=4):
        output = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(bytes(output)):
                return bytes(output)
            ready, _, _ = select.select([master], [], [], 0.02)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    if child.poll() is not None and predicate(bytes(output)):
                        return bytes(output)
                    break
            if child.poll() is not None and not ready:
                if predicate(bytes(output)):
                    return bytes(output)
                break
        plain = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([^)]|\)[^)])", b"", bytes(output))
        found = re.search(rb"static|effects|preview|stop", plain, re.I)
        context = plain[max(0, found.start()-120):found.start()+350] if found else None
        self.fail(f"PTY condition not met; exit={child.poll()}, match={context!r}, text tail={plain[-500:]!r}")

    def _assert_pty_quota(self, trace, *, minimum_writes, maximum_writes):
        events = self.observed_wmi(trace)
        self.assertEqual(sum(kind == "I" for _, kind in events) % 2, 0, events)
        writes = sum(kind == "I" for _, kind in events) // 2
        self.assertGreaterEqual(writes, minimum_writes, events)
        self.assertLessEqual(writes, maximum_writes, events)
        for at, _ in events:
            self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                     for when, _ in events), 49, events)
        return writes

    def _assert_pty_effect(self, rate, *, packaged):
        child, master, trace = self._pty_frontend(rate, packaged=packaged)
        # Wait for the actual curses editor before sending physical key bytes.
        opening = self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        os.write(master, b"\t")  # Tab selects first animated effect and starts it.
        activated = self._pty_until(child, master,
                                    lambda data: (self.root / "colors").read_bytes()
                                    != BASE + b"\n" and b"Stop" in data)
        self.assertIn(b"S Stop", activated)
        self.assertIn(b"EFFECT ACTIVE", activated)
        self.assertTrue((self.root / "normal-bounded-used").exists())
        first_frame = (self.root / "colors").read_bytes()
        self.assertNotEqual(first_frame, BASE + b"\n")
        os.write(master, b"s")
        stopped = self._pty_until(child, master,
                                  lambda data: (self.root / "colors").read_bytes()
                                  == BASE + b"\n" and b"restored; Stop" in data)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0, (opening, activated, stopped))
        # Read the fake-kernel attribute independently of the TUI/backend.
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        self._assert_pty_quota(trace, minimum_writes=2, maximum_writes=11)

    def test_packaged_4hz_curses_pty_tab_start_explicit_stop_and_quit(self):
        self._assert_pty_effect(4, packaged=True)

    def _assert_packaged_named_effect_starts_without_tab(self, rate, effect):
        child, master, trace = self._pty_frontend(rate, packaged=True,
                                                  test_effect=effect)
        opening = self._pty_until(child, master,
                                  lambda data: f"EFFECT {effect.upper()}".encode() in data)
        self.assertNotIn(b"EFFECT ACTIVE", opening)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertFalse((self.root / "normal-bounded-used").exists())
        os.write(master, b"a")  # No Tab: Start the locally selected effect.
        active = self._pty_until(child, master,
                                 lambda data: b"EFFECT ACTIVE" in data and
                                 (self.root / "colors").read_bytes() != BASE + b"\n")
        self.assertIn(b"S Stop", active)
        self.assertTrue((self.root / "normal-bounded-used").exists())
        os.write(master, b"s")
        self._pty_until(child, master,
                        lambda data: b"restored; Stop" in data and
                        (self.root / "colors").read_bytes() == BASE + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        self._assert_pty_quota(trace, minimum_writes=2, maximum_writes=2 * rate + 1)
        self.assertEqual(self.request(f"2 BURST {rate}\n".encode()), b"ERR BUSY\n")

    def test_packaged_4hz_wave_a_starts_directly_and_restores(self):
        self._assert_packaged_named_effect_starts_without_tab(4, "Wave")

    def test_packaged_5hz_scanner_a_starts_directly_and_restores(self):
        self._assert_packaged_named_effect_starts_without_tab(5, "Scanner")

    def test_packaged_long_four_hz_scanner_runs_past_short_limit_then_stops(self):
        child, master, trace = self._pty_frontend(
            4, packaged=True, test_effect="Scanner", long_session=True)
        self._pty_until(child, master, lambda data: b"EFFECT SCANNER" in data)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        os.write(master, b"a")
        active = self._pty_until(child, master,
                                 lambda data: b"EFFECT ACTIVE" in data and
                                 (self.root / "colors").read_bytes() != BASE + b"\n")
        self.assertIn(b"S Stop", active)
        self.assertTrue((self.root / "long-4hz-diagnostic-used").exists())
        self.assertFalse((self.root / "normal-bounded-used").exists())
        time.sleep(2.5)
        self.assertIsNone(child.poll())
        self.assertNotEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertGreaterEqual(sum(kind == "I" for _, kind in self.observed_wmi(trace)), 10)
        os.write(master, b"s")
        self._pty_until(child, master,
                        lambda data: b"restored; Stop" in data and
                        (self.root / "colors").read_bytes() == BASE + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        self._assert_pty_quota(trace, minimum_writes=6, maximum_writes=41)
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR BUSY\n")

    def test_packaged_long_session_refuses_normal_broker_without_writing(self):
        normal = pathlib.Path(self.bin_tmp.name) / "ordinary-for-long-refusal"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        child, master, trace = self._pty_frontend(
            4, packaged=True, test_effect="Wave", long_session=True,
            installed_broker=normal)
        self._pty_until(child, master, lambda data: b"EFFECT WAVE" in data)
        os.write(master, b"a")
        self._pty_until(child, master,
                        lambda data: b"needs the dedicated broker" in data)
        self.assertFalse((self.root / "normal-bounded-used").exists())
        self.assertFalse((self.root / "long-4hz-diagnostic-used").exists())
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)

    def test_fake_installed_real_broker_matches_make_and_zipapp_stops_bounded_effect(self):
        from test_installer_security import InstallerSafetyTests

        with InstallerSafetyTests().isolated(broker_root=self.root,
                                             broker_socket=self.sockpath) as (root, repo, prefix, run):
            result = run("install-dkms.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = prefix / "usr/local/libexec/okeylitctl-broker"
            archive = prefix / "usr/local/bin/okeylitctl"
            built = repo / "build/okeylitctl-broker"
            subprocess.run(["make", "-C", str(repo), "broker",
                            f"CC={root / 'commands/cc'}"], check=True,
                           capture_output=True, text=True)
            self.assertEqual(hashlib.sha256(installed.read_bytes()).digest(),
                             hashlib.sha256(built.read_bytes()).digest(),
                             "fake-installed broker differs from normal Makefile broker")
            child, master, trace = self._pty_frontend(
                4, packaged=True, installed_broker=installed, installed_archive=archive)
            self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
            os.write(master, b"\t")
            active = self._pty_until(child, master,
                                     lambda data: b"EFFECT ACTIVE" in data and
                                     (self.root / "colors").read_bytes() != BASE + b"\n")
            self.assertIn(b"S Stop", active)
            self.assertTrue((self.root / "normal-bounded-used").exists())
            os.write(master, b"s")
            self._pty_until(child, master,
                            lambda data: b"restored; Stop" in data and
                            (self.root / "colors").read_bytes() == BASE + b"\n")
            os.write(master, b"q")
            self._pty_until(child, master, lambda _: child.poll() is not None)
            self.assertEqual(child.returncode, 0)
            self.assertEqual((self.root / "colors").read_bytes(),
                             (self.root / "original").read_bytes())
            self._assert_pty_quota(trace, minimum_writes=2, maximum_writes=11)
            self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR BUSY\n")

    def test_direct_5hz_curses_pty_tab_start_explicit_stop_and_quit(self):
        self._assert_pty_effect(5, packaged=False)

    def test_packaged_5hz_curses_pty_auto_stop_after_bounded_lease(self):
        child, master, trace = self._pty_frontend(5, packaged=True)
        self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        started = time.monotonic()
        os.write(master, b"\t")
        output = self._pty_until(child, master,
                                 lambda data: b"verified frames" in data and
                                 (self.root / "colors").read_bytes() == BASE + b"\n",
                                 timeout=5)
        self.assertIn(b"Stop", output)
        self.assertIn(b"verified frames", output)
        self.assertTrue((self.root / "normal-bounded-used").exists())
        writes = self._assert_pty_quota(trace, minimum_writes=3, maximum_writes=11)
        # The lease cannot silently resume writing after auto-Stop.
        time.sleep(0.35)
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self._assert_pty_quota(trace, minimum_writes=writes,
                                               maximum_writes=writes), writes)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        time.sleep(max(0, started + 3.2 - time.monotonic()))
        # Independently read through the broker after its three-second lease;
        # the one-shot lease cannot be rearmed by a fresh client.
        self.assertEqual(self.request(b"1 STATUS\n"),
                         b"OK on " + BASE + b" " + BASE + b"\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR BUSY\n")
        self.assertEqual(self._assert_pty_quota(trace, minimum_writes=writes,
                                               maximum_writes=writes), writes)

    def test_direct_4hz_curses_pty_quit_restores_without_stop_key(self):
        child, master, trace = self._pty_frontend(4, packaged=False)
        self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        os.write(master, b"\t")
        self._pty_until(child, master,
                        lambda data: b"Stop" in data and
                        (self.root / "colors").read_bytes() != BASE + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        self._assert_pty_quota(trace, minimum_writes=2, maximum_writes=11)

    def test_pty_competing_cas_before_stop_preserves_newer_layout(self):
        # One end-to-end race at both the reference canvas and narrow minimum;
        # both entrypoints must use the same broker ownership boundary.
        for size, packaged in (((55, 160), True), ((24, 78), False)):
            with self.subTest(size=size, packaged=packaged):
                self.put("colors", BASE + b"\n")
                (self.root / "normal-bounded-used").unlink(missing_ok=True)
                self._assert_pty_competing_cas(size=size, packaged=packaged)

    def _assert_pty_competing_cas(self, *, size, packaged):
        def visible(data):
            return re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([^)]|\)[^)])", b"", data)

        child, master, trace = self._pty_frontend(4, packaged=packaged, size=size)
        self._pty_until(child, master, lambda data: b"STATIC" in visible(data))
        os.write(master, b"\t")
        self._pty_until(child, master,
                        lambda data: b"EFFECT ACTIVE" in visible(data) and
                        (self.root / "colors").read_bytes() != BASE + b"\n")
        frame = (self.root / "colors").read_bytes().strip()
        self.assertNotEqual(frame, BASE)
        # A separate authorized broker client wins the revision while the
        # foreground TUI still has a verified non-base frame to restore.
        token, state, colors, _ = self.snapshot()
        self.assertEqual((state, colors), (b"on", frame))
        self.assertRegex(self.cas(token, expected=frame, desired=ALT),
                         rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        writes_after_competitor = sum(kind == "I" for _, kind in self.observed_wmi(trace))

        os.write(master, b"s")
        stopped = visible(self._pty_until(child, master,
                                          lambda data: b"ownership lost" in visible(data).lower()
                                          and (size[1] == 78 or b"LAST OBSERVED" in visible(data))))
        if size[1] != 78:
            self.assertIn(b"LAST OBSERVED", stopped)
        self.assertNotIn(b"IN SYNC", stopped)
        self.assertNotIn(b"S Restore", stopped)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        self.assertEqual(sum(kind == "I" for _, kind in self.observed_wmi(trace)),
                         writes_after_competitor)

        time.sleep(1.2)  # Ordinary read quota rolls forward after Stop contention.
        os.write(master, b"r")  # read-only; never regain CAS ownership
        refreshed = visible(self._pty_until(child, master,
                                            lambda data: (b"Last observed layout verified" in visible(data)
                                                          or b"Live unknown" in visible(data))))
        self.assertIn(b"Last observed layout verified", refreshed[-500:])
        self.assertNotIn(b"IN SYNC", refreshed)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        os.write(master, b"q")
        self._pty_until(child, master, lambda data: b"Quit?" in visible(data))
        os.write(master, b"y")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        self.assertEqual(sum(kind == "I" for _, kind in self.observed_wmi(trace)),
                         writes_after_competitor)

    def test_packaged_4hz_curses_pty_interrupt_restores_active_frame(self):
        child, master, trace = self._pty_frontend(4, packaged=True)
        self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        os.write(master, b"\t")
        self._pty_until(child, master,
                        lambda data: b"Stop" in data and
                        (self.root / "colors").read_bytes() != BASE + b"\n")
        os.kill(child.pid, signal.SIGINT)
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())
        self._assert_pty_quota(trace, minimum_writes=2, maximum_writes=11)

    def test_direct_5hz_curses_pty_readback_failure_keeps_editor_locked(self):
        child, master, trace = self._pty_frontend(5, packaged=False, fake_mode="mismatch")
        self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        os.write(master, b"\t")
        output = self._pty_until(child, master,
                                 lambda data: b"Effect I/O failed" in data and
                                 b"Verify  ? help" in data)
        self.assertIn(b"EFFECT UNKNOWN", output)
        self.assertIn(b"S Verify before any restore", output)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual(self._assert_pty_quota(trace, minimum_writes=1,
                                               maximum_writes=1), 1)
        os.write(master, b"s")  # read-only reconciliation; no blind retry
        observed = self._pty_until(child, master,
                                   lambda data: b"state remains uncertain" in data,
                                   timeout=3)
        self.assertIn(b"restoration is blocked", observed)
        self.assertEqual(self._assert_pty_quota(trace, minimum_writes=1,
                                               maximum_writes=1), 1)
        os.write(master, b"q")
        time.sleep(0.1)
        self.assertIsNone(child.poll(), "unknown effect must not silently exit")
        self.assertEqual(self._assert_pty_quota(trace, minimum_writes=1,
                                               maximum_writes=1), 1)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())

    def test_packaged_4hz_curses_pty_power_off_refuses_start_without_writes(self):
        self.put("state", b"off\n")
        child, master, trace = self._pty_frontend(4, packaged=True)
        self._pty_until(child, master, lambda data: b"EFFECT STATIC" in data)
        os.write(master, b"\t")
        output = self._pty_until(child, master,
                                 lambda data: b"power off" in data.lower())
        self.assertIn(b"power off", output.lower())
        os.write(master, b"q")
        self._pty_until(child, master, lambda _: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self._assert_pty_quota(trace, minimum_writes=0, maximum_writes=0)
        self.assertEqual((self.root / "colors").read_bytes(),
                         (self.root / "original").read_bytes())

    def test_normal_tui_static_apply_stop_restore_baseline(self):
        import io
        from unittest import mock
        from okeylitctl.cli import main
        from okeylitctl.ipc import IPCBackend
        from test_tui import FakeProfileStore

        trace = self.wmi_trace()
        # The fake kernel must implement restore_store's effect on colors,
        # not merely count the write; otherwise this tests an impossible device.
        self.restart_with_fake_kernel("restore", trace=trace)

        class Screen:
            def __init__(self):
                self.keys = iter((ord("p"), ord("a"), ord("s"), ord("o"), ord("q")))
                self.applied = None
                self.stopped = None
                self.restored = None

            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == ord("s"):
                    self.applied = (root / "colors").read_bytes()
                if key == ord("o"):
                    self.stopped = (root / "colors").read_bytes()
                if key == ord("q"):
                    self.restored = (root / "colors").read_bytes()
                return key

        root = self.root
        screen = Screen()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("okeylitctl.cli.IPCBackend",
                        side_effect=lambda: IPCBackend(socket_path=self.sockpath)), \
             mock.patch("okeylitctl.tui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            code = main(["tui"], profile_store=FakeProfileStore(),
                        stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotEqual(screen.applied, BASE + b"\n")
        self.assertEqual(screen.stopped, screen.applied)
        self.assertEqual(screen.restored, BASE + b"\n")
        self.assertEqual((root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual(sum(kind == "I" for _, kind in self.observed_wmi(trace)), 4)

    def test_tui_stop_recovers_from_one_slow_kernel_read_before_next_key(self):
        for rate in (4, 5):
            with self.subTest(rate=rate):
                self._run_tui_stop_after_one_slow_read(rate)

    def _run_tui_stop_after_one_slow_read(self, rate):
        import io
        import threading
        from unittest import mock
        from okeylitctl.cli import main
        from okeylitctl.ipc import IPCBackend
        from okeylitctl.tui import CursesTui
        from test_tui import FakeProfileStore

        normal = pathlib.Path(self.bin_tmp.name) / "normal-slow-tui-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        (self.root / "normal-bounded-used").unlink(missing_ok=True)
        trace = pathlib.Path(self.tmp.name) / f"normal-slow-tui-{rate}-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)

        class Screen:
            def __init__(self, test):
                self.test = test
                self.keys = iter((9, -1, ord("s"), ord("q")))
                self.stop_elapsed = None
                self.stops = []

            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == -1:
                    time.sleep(0.05)  # below the next-tick deadline
                    self.frame = (self.test.root / "colors").read_bytes()
                    self.test.assertNotEqual(self.frame, BASE + b"\n")
                    pathlib.Path(str(trace) + ".slow-once").touch()
                    self.started = time.monotonic()
                    def competing_statuses():
                        time.sleep(0.1)  # queue behind the delayed Stop read
                        self.statuses = [self.test.request(b"1 STATUS\n")
                                         for _ in range(2)]
                    self.competitor = threading.Thread(target=competing_statuses)
                    self.competitor.start()
                if key == ord("q"):
                    self.stop_elapsed = time.monotonic() - self.started
                return key

        screen = Screen(self)
        original_stop = CursesTui._stop_effect
        def observed_stop(app):
            result = original_stop(app)
            screen.stops.append((result, (self.root / "colors").read_bytes(),
                                 app.state.effect_restore_pending))
            return result
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("okeylitctl.cli.IPCBackend",
                        side_effect=lambda: IPCBackend(socket_path=self.sockpath)), \
             mock.patch("okeylitctl.tui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
             mock.patch.object(CursesTui, "_stop_effect", observed_stop), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            code = main(["tui", "--test-rate-hz", str(rate), "--rate-experiment"],
                        profile_store=FakeProfileStore(), stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        screen.competitor.join(timeout=4)
        self.assertFalse(screen.competitor.is_alive())
        self.assertTrue(all(reply.startswith(b"OK on ") or reply == b"ERR BUSY\n"
                            for reply in screen.statuses), screen.statuses)
        self.assertEqual(screen.stops[0], (True, BASE + b"\n", False), screen.stops)
        self.assertLess(screen.stop_elapsed, 3.0)
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(sum(kind == "I" for _, kind in events), 4)  # frame + restore
        for at, _ in events:
            self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                     for when, _ in events), 49, events)

    def test_tui_persistent_slow_stop_keeps_frame_and_editor_locked(self):
        import io
        from unittest import mock
        from okeylitctl.cli import main
        from okeylitctl.ipc import IPCBackend
        from okeylitctl.tui import CursesTui
        from test_tui import FakeProfileStore

        normal = pathlib.Path(self.bin_tmp.name) / "normal-persistent-tui-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_BOUNDED_NORMAL",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(normal)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = normal
        trace = pathlib.Path(self.tmp.name) / "normal-persistent-tui-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)

        class Screen:
            keys = iter((9, -1, ord("s")))
            frame = None
            stops = []
            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == -1:
                    time.sleep(0.05)
                    self.frame = (root / "colors").read_bytes()
                    pathlib.Path(str(trace) + ".slow").touch()
                return key

        root = self.root
        screen = Screen()
        original_stop = CursesTui._stop_effect
        def observed_stop(app):
            result = original_stop(app)
            screen.stops.append((result, app.state.effect_running,
                                 app.state.effect_restore_pending,
                                 app.effect_runtime is not None,
                                 (root / "colors").read_bytes()))
            return result
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch("okeylitctl.cli.IPCBackend",
                            side_effect=lambda: IPCBackend(socket_path=self.sockpath, timeout=2.5)), \
                 mock.patch("okeylitctl.tui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
                 mock.patch.object(CursesTui, "_stop_effect", observed_stop), \
                 mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
                code = main(["tui", "--test-rate-hz", "4", "--rate-experiment"],
                            profile_store=FakeProfileStore(), stdout=out, stderr=err)
        finally:
            pathlib.Path(str(trace) + ".slow").unlink(missing_ok=True)
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotEqual(screen.frame, BASE + b"\n")
        self.assertGreaterEqual(len(screen.stops), 2)
        self.assertTrue(all(stop == (False, True, True, True, screen.frame)
                            for stop in screen.stops), screen.stops)
        self.assertEqual(sum(kind == "I" for _, kind in self.observed_wmi(trace)), 2)

    def test_candidate_saturated_status_clients_leave_priority_stop_headroom(self):
        candidate = pathlib.Path(self.bin_tmp.name) / "candidate-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_SUSTAINED_CANDIDATE",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(candidate)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = candidate
        trace = pathlib.Path(self.tmp.name) / "saturated-candidate-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)
        expected, desired = BASE, ALT
        started = time.monotonic()
        for index in range(5):
            due = started + index * 0.2
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            self.assertTrue(self.cas(token, expected=expected,
                                     desired=desired).startswith(b"OK "))
            expected, desired = desired, expected
        # Three competing readers may be refused, but cannot take Stop's
        # reserved snapshot admission or make it wait for a rolling expiry.
        statuses = [self.request(b"1 STATUS\n") for _ in range(3)]
        self.assertEqual(sum(reply.startswith(b"OK on ") for reply in statuses), 2,
                         statuses)
        self.assertEqual(statuses.count(b"ERR BUSY\n"), 1, statuses)
        began = time.monotonic()
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertLess(time.monotonic() - began, 0.12)
        self.assertEqual(stop.split(b" ")[3], expected)
        self.assertNotEqual(stop.split(b" ")[1], token)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(len(events), 5 * 7 +
                         2 * sum(reply.startswith(b"OK ") for reply in statuses) + 2 + 5)
        for at, _ in events:
            window = [kind for when, kind in events
                      if at <= when < at + 1_000_000_000]
            self.assertLessEqual(len(window), 49, events)
            self.assertLessEqual(window.count("I"), 12, events)
        self.binary = self.__class__.binary

    def test_candidate_stop_token_cannot_restore_after_external_write(self):
        candidate = pathlib.Path(self.bin_tmp.name) / "candidate-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_SUSTAINED_CANDIDATE",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(candidate)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = candidate
        trace = pathlib.Path(self.tmp.name) / "candidate-owner-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)
        first = self.snapshot()[0]
        self.assertTrue(self.cas(first).startswith(b"OK "))
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        third = b"010203,040506,070809,0A0B0C"
        self.assertEqual(self.request(b"1 SET " + third + b"\n"), b"OK\n")
        self.assertEqual(self.cas(stop.split(b" ")[1], expected=ALT, desired=BASE),
                         b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), third + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(len(events), 2 + 5 + 2 + 4)
        self.assertEqual([kind for _, kind in events[-4:]], ["S", "I", "I", "C"])
        self.binary = self.__class__.binary

    def test_candidate_slow_stop_read_times_out_without_restoration(self):
        candidate = pathlib.Path(self.bin_tmp.name) / "candidate-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_SUSTAINED_CANDIDATE",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(candidate)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = candidate
        trace = pathlib.Path(self.tmp.name) / "candidate-slow-wmi"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)
        token = self.snapshot()[0]
        self.assertTrue(self.cas(token).startswith(b"OK "))
        marker = pathlib.Path(str(trace) + ".slow")
        marker.touch()
        try:
            self.assertEqual(self.request(b"2 STOPSHOT\n"), b"ERR BUSY\n")
        finally:
            marker.unlink(missing_ok=True)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=ALT,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        for at, _ in events:
            self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                     for when, _ in events), 49, events)
        self.binary = self.__class__.binary

    def test_stop_read_timeout_preserves_ownership_before_competing_write(self):
        trace = self.wmi_trace()
        token = self.snapshot()[0]
        verified = self.cas(token)[3:-1]
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        marker = pathlib.Path(str(trace) + ".slow")
        third = b"010203,040506,070809,0A0B0C"
        marker.touch()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as stopping, \
                 socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as competitor:
                for client in (stopping, competitor):
                    client.settimeout(3)
                    client.connect(str(self.sockpath))
                began = time.monotonic()
                stopping.sendall(b"2 STOPSHOT\n")
                # Let the Stop read enter the fake kernel before the competing
                # authorized request arrives; it queues on the same broker.
                time.sleep(0.05)
                competitor.sendall(b"1 STATUS\n")
                self.assertEqual(stopping.recv(256), b"ERR BUSY\n")
                self.assertLess(time.monotonic() - began, 1.5)
                marker.unlink()
                try:
                    queued_reply = competitor.recv(256)
                except ConnectionResetError:
                    # The broker may close an expired unread packet with RST.
                    queued_reply = b"ERR BUSY\n"
                self.assertTrue(queued_reply == b"ERR BUSY\n" or
                                queued_reply.startswith(b"OK on "), queued_reply)
                self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        finally:
            marker.unlink(missing_ok=True)
        # The competing status is read-only even if serviced after Stop;
        # a fresh authorized mutation can change ownership before recovery.
        self.assertEqual(self.request(b"1 SET " + third + b"\n"), b"OK\n")
        response = self.request(b"2 STOPSHOT\n")
        self.assertTrue(response.startswith(b"OK "), response)
        self.assertEqual(response.split(b" ")[3], third)
        self.assertNotEqual(response.split(b" ")[1], verified)
        self.assertEqual(self.cas(verified, expected=ALT, desired=BASE),
                         b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), third + b"\n")
        events = self.observed_wmi(trace)
        expected_trace = ["C", "S", "S", "C", "I", "I", "C", "C", "S"]
        self.assertEqual([kind for _, kind in events[:9]], expected_trace)
        queued_kinds = [kind for _, kind in events[9:-6]]
        self.assertIn(queued_kinds, ([], ["C"], ["C", "S"]))
        if queued_reply.startswith(b"OK "):
            self.assertEqual(queued_kinds, ["C", "S"])
        self.assertEqual([kind for _, kind in events[-6:]],
                         ["S", "I", "I", "C", "C", "S"])
        for at, _ in events:
            self.assertLessEqual(sum(at <= when < at + 1_000_000_000
                                     for when, _ in events), 25, events)

    def test_default_broker_rejects_rate_experiment_without_elevating_reads(self):
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR INVALID\n")

    def start_long_diagnostic(self, *, trace=None):
        binary = pathlib.Path(self.bin_tmp.name) / "long-4hz-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_LONG_4HZ_DIAGNOSTIC",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(binary)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = binary
        self.launch_broker("trace" if trace else None, trace=trace)

    def test_long_four_hz_diagnostic_sustains_40_frames_12_seconds_and_owned_stop(self):
        makefile = (REPO / "Makefile").read_text()
        self.assertIn("broker-long-4hz-diagnostic: build/okeylitctl-broker-long-4hz-diagnostic", makefile)
        ordinary = makefile.split("build/okeylitctl-broker: broker/main.c")[1].split("\n\n")[0]
        self.assertNotIn("BROKER_LONG_4HZ_DIAGNOSTIC", ordinary)
        prior = {name: self.root / name for name in
                 ("normal-bounded-used", "rate-experiment-used",
                  "rate-experiment-2-used", "rate-experiment-3-used")}
        for stamp in prior.values():
            stamp.write_bytes(b"previous campaign")
        trace = pathlib.Path(self.tmp.name) / "long-4hz-wmi"
        trace.write_text("")
        self.start_long_diagnostic(trace=trace)
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 LONG4\n"), b"OK LONG4\n")
        stamp = self.root / "long-4hz-diagnostic-used"
        self.assertEqual(stamp.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(path.read_bytes() == b"previous campaign" for path in prior.values()))
        started = time.monotonic()
        third = b"010203,040506,070809,0A0B0C"
        expected = BASE
        for index in range(40):
            due = started + index * 0.25
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            token, state, colors, original = self.snapshot()
            self.assertEqual((state, colors, original), (b"on", expected, BASE))
            desired = ALT if index % 2 == 0 else third
            self.assertRegex(self.cas(token, expected=expected, desired=desired),
                             rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            expected = desired
        self.assertEqual(expected, third)
        remaining = started + 12 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        # Competing read-only clients fill their class while Stop retains two
        # status slots, five CAS slots, one write and seven total GET slots.
        for _ in range(8):
            reply = self.request(b"1 STATUS\n")
            if reply == b"ERR BUSY\n":
                break
            self.assertTrue(reply.startswith(b"OK on "), reply)
        else:
            self.fail("competing STATUS clients did not saturate their quota")
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertEqual(stop.split(b" ")[3], third)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=third,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(sum(kind == "I" for _, kind in events), 82)
        for timestamp, _ in events:
            window = [kind for at, kind in events if timestamp <= at < timestamp + 1_000_000_000]
            self.assertLessEqual(len(window), 49, window)
            self.assertLessEqual(window.count("I"), 12, window)
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR BUSY\n")
        remaining = started + 15.15 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        time.sleep(1.1)  # Drain lease-era rolling reservations.
        for _ in range(5):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR BUSY\n")
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR BUSY\n")
        self.assertTrue(stamp.exists())

    def test_long_diagnostic_restart_cannot_rearm_and_default_remains_ordinary(self):
        self.start_long_diagnostic()
        self.assertEqual(self.request(b"2 LONG4\n"), b"OK LONG4\n")
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR BUSY\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR INVALID\n")
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = self.__class__.binary
        self.launch_broker()
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR INVALID\n")
        self.assertEqual(self.request(b"2 LONG4\n"), b"ERR INVALID\n")

    def test_long_diagnostic_fifth_write_waits_for_four_hz_and_preserves_stop(self):
        trace = pathlib.Path(self.tmp.name) / "long-4hz-fifth-write"
        trace.write_text("")
        self.start_long_diagnostic(trace=trace)
        self.assertEqual(self.request(b"2 LONG4\n"), b"OK LONG4\n")
        started = time.monotonic()
        expected = BASE
        for index in range(4):
            token, state, colors, original = self.snapshot()
            self.assertEqual((state, colors, original), (b"on", expected, BASE))
            desired = ALT if index % 2 == 0 else BASE
            self.assertTrue(self.cas(token, expected=expected,
                                     desired=desired).startswith(b"OK "))
            expected = desired
        token, _, colors, _ = self.snapshot()
        self.assertEqual(colors, BASE)
        self.assertTrue(self.cas(token, expected=BASE, desired=ALT).startswith(b"OK "))
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        writes = [at for at, kind in self.observed_wmi(trace) if kind == "I"][::2]
        self.assertEqual(len(writes), 5)
        for before, after in zip(writes, writes[1:]):
            self.assertGreaterEqual(after - before, 250_000_000)
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=ALT,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_campaign_two_lease_does_not_consume_legacy_stamp(self):
        legacy = self.root / "rate-experiment-used"
        legacy.write_bytes(b"prior trial")
        self.start_diagnostic()
        self.assertEqual(self.request(b"2 BURST 4\n"), b"OK BURST\n")
        self.assertEqual(legacy.read_bytes(), b"prior trial")
        self.assertTrue((self.root / "rate-experiment-2-used").exists())

    def test_campaign_three_isolated_from_both_consumed_campaigns(self):
        prior = {name: self.root / name for name in
                 ("rate-experiment-used", "rate-experiment-2-used")}
        for stamp in prior.values():
            stamp.write_bytes(b"consumed prior campaign")
        self.start_diagnostic(campaign=3)
        self.assertEqual(self.request(b"2 BURST 4\n"), b"OK BURST\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR BUSY\n")
        self.assertTrue((self.root / "rate-experiment-3-used").exists())
        self.assertTrue(all(p.read_bytes() == b"consumed prior campaign"
                            for p in prior.values()))
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR BUSY\n")
        self.assertTrue(all(p.read_bytes() == b"consumed prior campaign"
                            for p in prior.values()))

    def test_diagnostic_lease_cannot_be_rearmed_by_service_restart(self):
        self.start_diagnostic()
        self.assertEqual(self.request(b"2 BURST 4\n"), b"OK BURST\n")
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker()
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR BUSY\n")

    def test_diagnostic_four_and_five_hz_trace_tenth_frame_stop_and_contention(self):
        third = b"010203,040506,070809,0A0B0C"
        for rate in (4, 5):
            with self.subTest(rate=rate):
                self.start_diagnostic()
                trace = pathlib.Path(self.tmp.name) / f"burst-{rate}-trace"
                trace.write_text("")
                self.restart_with_fake_kernel("trace", trace=trace)
                self.assertEqual(self.request(f"2 BURST {rate}\n".encode()), b"OK BURST\n")
                expected, desired = BASE, ALT
                started = time.monotonic()
                for index in range(10):
                    target = started + index / rate
                    if target > time.monotonic():
                        time.sleep(target - time.monotonic())
                    token, state, colors, _ = self.snapshot()
                    self.assertEqual((state, colors), (b"on", expected))
                    if index == 4:
                        # Another authorized client consumes status slots while the
                        # effect holds ownership; it must not consume Stop reserve.
                        for _ in range(2):
                            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
                    self.assertTrue(self.cas(token, expected=expected, desired=desired).startswith(b"OK "))
                    expected, desired = desired, third if desired == ALT else ALT
                stop = self.request(b"2 STOPSHOT\n")
                self.assertTrue(stop.startswith(b"OK "), stop)
                self.assertEqual(stop.split(b" ")[3], expected)
                self.assertNotEqual(expected, BASE)
                self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                         desired=BASE).startswith(b"OK "))
                events = self.observed_wmi(trace)
                self.assertEqual(len(events), 10 * 7 + 2 * 2 + 2 + 5)
                for timestamp, _ in events:
                    self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                             for at, _ in events), 49)
                self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
                self.proc.terminate()
                self.proc.communicate(timeout=2)
                self.binary = self.__class__.binary
                self.launch_broker()
                (self.root / "rate-experiment-2-used").unlink()

    def start_diagnostic(self, campaign=2):
        if campaign not in (2, 3):
            raise ValueError("unknown diagnostic campaign")
        diagnostic = pathlib.Path(self.bin_tmp.name) / "diagnostic-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_RATE_EXPERIMENT", f"-DBROKER_RATE_CAMPAIGN_{campaign}",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(diagnostic)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = diagnostic
        self.launch_broker()

    def test_explicit_diagnostic_broker_bounds_five_hz_and_preserves_stop(self):
        diagnostic = pathlib.Path(self.bin_tmp.name) / "diagnostic-broker"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DBROKER_TESTING", "-DBROKER_RATE_EXPERIMENT", "-DBROKER_RATE_CAMPAIGN_2",
                        f'-DBROKER_TEST_ROOT="{self.root}"',
                        f'-DBROKER_TEST_SOCKET="{self.sockpath}"',
                        str(REPO / "broker" / "main.c"), "-o", str(diagnostic)], check=True)
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.binary = diagnostic
        trace = pathlib.Path(self.tmp.name) / "burst-wmi-trace"
        trace.write_text("")
        self.launch_broker("trace", trace=trace)
        self.assertEqual(self.request(b"2 BURST 5\n"), b"OK BURST\n")
        self.assertEqual(self.request(b"2 BURST 5\n"), b"ERR BUSY\n")
        expected, desired = BASE, ALT
        started = time.monotonic()
        for index in range(9):
            target = started + index * 0.2
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            reply = self.cas(token, expected=expected, desired=desired)
            self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            expected, desired = desired, expected
        stop = self.request(b"2 STOPSHOT\n")
        self.assertTrue(stop.startswith(b"OK "), stop)
        self.assertEqual(expected, ALT)
        self.assertTrue(self.cas(stop.split(b" ")[1], expected=expected,
                                 desired=BASE).startswith(b"OK "))
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(len(events), 70)
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 49)
        # The broker lease is one-shot even after its three-second deadline.
        remaining = started + 3.1 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.assertEqual(self.request(b"2 BURST 4\n"), b"ERR BUSY\n")
        for _ in range(5):
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")

    def test_two_hz_stop_snapshot_restores_with_cas(self):
        trace = self.wmi_trace()
        start = time.monotonic()
        expected, desired = BASE, ALT
        for step in range(5):
            target = start + step * 0.51
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            reply = self.cas(token, expected=expected, desired=desired)
            self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "))
            expected, desired = desired, expected
        began = time.monotonic()
        response = self.request(b"2 STOPSHOT\n")
        match = re.fullmatch(rb"OK ([0-9A-F]{16}:[0-9A-F]{16}) on (" +
                             ALT + rb") " + BASE + rb"\n", response)
        self.assertIsNotNone(match, response)
        self.assertLess(time.monotonic() - began, 1.0)
        reply = self.cas(match.group(1), expected=ALT, desired=BASE)
        self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        events = self.observed_wmi(trace)
        self.assertEqual(len(events), 52)  # five frames, competing STATUS, Stop
        for timestamp, _ in events:
            self.assertLessEqual(sum(timestamp <= at < timestamp + 1_000_000_000
                                     for at, _ in events), 25)

    def test_stop_snapshot_does_not_restore_after_competing_write(self):
        token = self.snapshot()[0]
        self.assertTrue(self.cas(token).startswith(b"OK "))
        response = self.request(b"2 STOPSHOT\n")
        stop_token = response.split(b" ")[1]
        self.assertEqual(self.request(b"1 SET " + BASE + b"\n"), b"OK\n")
        self.assertEqual(self.cas(stop_token, expected=ALT, desired=BASE),
                         b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_two_hz_legacy_status_and_cas_keep_readback_budget(self):
        expected, desired = BASE, ALT
        token = self.snapshot()[0]
        start = time.monotonic()
        for step in range(6):
            target = start + (step + 1) * 0.51
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            self.assertEqual(self.request(b"1 STATUS\n"),
                             b"OK on " + expected + b" " + BASE + b"\n")
            reply = self.cas(token, expected=expected, desired=desired)
            self.assertRegex(reply, rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            token = reply[3:-1]
            expected, desired = desired, expected

    def test_cas_rechecks_colors_after_write_slot_wait(self):
        token = self.snapshot()[0]
        start = time.monotonic()
        for step in range(5):
            target = start + step * 0.2
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        token = self.snapshot()[0]
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(3)
            client.connect(str(self.sockpath))
            client.sendall(b"2 CAS " + token + b" on " + ALT + b" " + ALT + b"\n")
            time.sleep(0.05)  # the broker is waiting for a write slot
            self.put("colors", BASE + b"\n")
            self.assertEqual(client.recv(256), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_off_state_cas_refuses_write_without_invalidating_token(self):
        self.put("state", b"off\n")
        token, state, _, _ = self.snapshot()
        self.assertEqual(state, b"off")
        self.assertEqual(self.cas(token, state=b"off"), b"ERR CONFLICT\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual(self.snapshot()[0], token)

    def test_uncertain_cas_invalidates_token(self):
        self.restart_with_fake_kernel("mismatch")
        token = self.snapshot()[0]
        self.assertEqual(self.cas(token),
                         b"ERR IO\n")
        self.assertNotEqual(self.snapshot()[0], token)

    def test_status_and_set_readback_and_restore(self):
        self.assertEqual(self.request(b"1 STATUS\n"), b"OK on " + BASE + b" " + BASE + b"\n")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")
        self.assertEqual(self.request(b"1 RESTORE\n"), b"ERR IO\n")
        self.assertEqual((self.root / "restore").read_bytes(), b"1\n")

    def test_restore_acknowledges_only_verified_kernel_restoration(self):
        self.restart_with_fake_kernel("restore")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        self.assertEqual(self.request(b"1 RESTORE\n"), b"OK\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.assertEqual((self.root / "restore").read_bytes(), b"1\n")

    def test_post_write_mismatch_reports_uncertain_io(self):
        self.restart_with_fake_kernel("mismatch")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR IO\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_post_write_malformed_readback_reports_uncertain_io(self):
        self.restart_with_fake_kernel("malformed")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR IO\n")
        self.assertEqual((self.root / "colors").read_bytes(), b"bad\n")

    def test_strict_packets_never_write(self):
        invalid = [b"1 SET " + ALT.lower() + b"\n", b"1 SET " + ALT + b"\r\n",
                   b"1 SET " + ALT + b"\nextra", b"1 SET " + ALT[:-1] + b"\n",
                   b"1 SET " + ALT + b"\x00\n", b"1 SET " + ALT + b"\n" + b"X" * 128,
                   b"1 STATUS\nextra", b"1 STATUS", b"2 STATUS\n",
                   b"1 RESTORE \n", b"1 SET " + ALT.replace(b",", b";") + b"\n"]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.request(payload), b"ERR INVALID\n")
                self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        # Queue both records while the single-threaded broker serves a stalled peer.
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as blocker:
            blocker.settimeout(3)
            blocker.connect(str(self.sockpath))
            time.sleep(0.02)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
                client.settimeout(3)
                client.connect(str(self.sockpath))
                client.sendall(b"1 SET " + ALT + b"\n")
                client.sendall(b"1 RESTORE\n")
                self.assertEqual(blocker.recv(256), b"ERR BUSY\n")
                self.assertEqual(client.recv(256), b"ERR INVALID\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_symlink_and_nonregular_attribute_rejected(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.write_bytes(BASE + b"\n")
        colors = self.root / "colors"
        colors.unlink()
        colors.symlink_to(outside)
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR DENIED\n")
        self.assertEqual(outside.read_bytes(), BASE + b"\n")
        colors.unlink()
        colors.mkdir()
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR DENIED\n")

    def test_denied_writes_cannot_deplete_verification_reserve(self):
        colors = self.root / "colors"
        colors.unlink()
        colors.symlink_to(self.root / "original")
        for _ in range(15):
            self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR DENIED\n")
        colors.unlink()
        self.put("colors", BASE + b"\n")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")

    def test_world_writable_root_and_symlinked_read_are_rejected(self):
        self.root.chmod(0o777)
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR DENIED\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.root.chmod(0o755)
        outside = pathlib.Path(self.tmp.name) / "outside-state"
        outside.write_bytes(b"on\n")
        (self.root / "state").unlink()
        (self.root / "state").symlink_to(outside)
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR DENIED\n")

    def test_abi_and_read_validation_fail_closed(self):
        self.put("abi_version", b"2\n")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR ABI\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")
        self.put("abi_version", b"1\n")
        self.put("original", b"not-colors\n")
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR ABI\n")
        self.assertEqual(self.request(b"1 RESTORE\n"), b"ERR ABI\n")
        self.put("original", BASE + b"\n")
        self.put("state", b"unknown\n")
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR ABI\n")

    def test_missing_sysfs_newline_is_not_valid_abi(self):
        self.put("abi_version", b"1")
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR ABI\n")
        self.put("abi_version", b"1\n")
        self.put("colors", BASE)
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR ABI\n")
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        self.put("state", b"on")
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR ABI\n")

    def test_missing_module_and_write_mode_rejection(self):
        (self.root / "abi_version").unlink()
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR MODULE\n")
        self.put("abi_version", b"1\n")
        os.chmod(self.root / "colors", 0o666)
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"ERR DENIED\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_full_write_window_waits_for_slot_before_stop(self):
        start = time.monotonic()
        for step in range(5):
            target = start + step * 0.2
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        began = time.monotonic()
        self.assertEqual(self.request(b"1 SET " + BASE + b"\n"), b"OK\n")
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 0.1)  # no sixth write inside the sliding second
        self.assertLess(elapsed, 0.7)  # bounded restoration, not an immediate BUSY
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_disconnected_client_cannot_mutate_after_waiting_for_slot(self):
        start = time.monotonic()
        for step in range(5):
            target = start + step * 0.2
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as abandoned:
            abandoned.connect(str(self.sockpath))
            abandoned.sendall(b"1 SET " + BASE + b"\n")
            time.sleep(0.05)  # broker enters the quota wait
        time.sleep(0.4)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")

    def test_global_firmware_read_budget(self):
        for _ in range(5):
            self.assertEqual(self.request(b"1 STATUS\n"), b"OK on " + BASE + b" " + BASE + b"\n")
        self.assertEqual(self.request(b"1 STATUS\n"), b"ERR BUSY\n")
        # STATUS cannot consume the quota reserved for post-write verification.
        self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n")
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")

    def test_two_hz_status_and_set_sustain_verified_updates(self):
        start = time.monotonic()
        for step in range(6):
            target = start + step * 0.51
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            self.assertTrue(self.request(b"1 STATUS\n").startswith(b"OK on "), step)
            self.assertEqual(self.request(b"1 SET " + ALT + b"\n"), b"OK\n", step)
        self.assertEqual((self.root / "colors").read_bytes(), ALT + b"\n")

    def test_client_stall_times_out_and_followup_works(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(3)
            client.connect(str(self.sockpath))
            self.assertEqual(client.recv(256), b"ERR BUSY\n")
        self.assertEqual(self.request(b"1 STATUS\n"), b"OK on " + BASE + b" " + BASE + b"\n")

    def test_idle_client_does_not_block_ready_client(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as idle:
            idle.settimeout(3)
            idle.connect(str(self.sockpath))
            time.sleep(0.03)  # let the broker accept the idle connection first
            started = time.monotonic()
            self.assertEqual(self.request(b"1 STATUS\n"),
                             b"OK on " + BASE + b" " + BASE + b"\n")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(idle.recv(256), b"ERR BUSY\n")

    def test_queued_mutations_are_served_in_accept_order(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as first, \
             socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as second:
            first.settimeout(3)
            second.settimeout(3)
            first.connect(str(self.sockpath))
            first.sendall(b"1 SET " + ALT + b"\n")
            second.connect(str(self.sockpath))
            second.sendall(b"1 SET " + BASE + b"\n")
            self.assertEqual(first.recv(256), b"OK\n")
            self.assertEqual(second.recv(256), b"OK\n")
        self.assertEqual((self.root / "colors").read_bytes(), BASE + b"\n")

    def test_abandoned_queued_mutation_is_not_applied(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as blocker:
            blocker.settimeout(3)
            blocker.connect(str(self.sockpath))
            time.sleep(0.02)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as abandoned:
                abandoned.connect(str(self.sockpath))
                abandoned.sendall(b"1 SET " + ALT + b"\n")
            self.assertEqual(blocker.recv(256), b"ERR BUSY\n")
            self.assertEqual(self.request(b"1 STATUS\n"), b"OK on " + BASE + b" " + BASE + b"\n")


from okeylitctl.effects import EffectKind


# A separate unittest fixture (fresh fake broker and unconsumed one-shot lease)
# is required for each effect; subtests inside one broker would be dishonest.
for _effect in EffectKind:
    if _effect is EffectKind.STATIC:
        continue
    for _rate in (4, 5):
        def _case(self, effect=_effect, rate=_rate):
            self._sweep_normal_tui_effect(effect, rate)

        setattr(BrokerIntegration,
                f"test_normal_tui_effect_{_effect.value.lower()}_{_rate}hz", _case)


class BrokerPackaging(unittest.TestCase):
    def test_expired_burst_cannot_overfill_downgraded_write_counter(self):
        (REPO / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="broker-budget-", dir=REPO / "build") as directory:
            source = pathlib.Path(directory) / "budget.c"
            binary = pathlib.Path(directory) / "budget"
            source.write_text('''#define main hidden_broker_main
#include "broker/main.c"
#undef main
int main(void) {
    write_count = 6;
    for (size_t i = 0; i < write_count; ++i) write_times[i] = now_ms();
    return reserve_slot(write_times, &write_count, WRITE_LIMIT) == BUSY &&
           write_count == 6 ? 0 : 1;
}
''')
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-DBROKER_RATE_EXPERIMENT", "-I", str(REPO), str(source),
                            "-o", str(binary)], check=True)
            self.assertEqual(subprocess.run([str(binary)]).returncode, 0)

    def test_sustained_candidate_cannot_compile_for_installed_broker(self):
        with tempfile.TemporaryDirectory(prefix="broker-candidate-", dir=REPO / "build") as directory:
            command = ["cc", "-std=c11", "-DBROKER_SUSTAINED_CANDIDATE",
                       str(REPO / "broker" / "main.c"), "-o", str(pathlib.Path(directory) / "broker")]
            failed = subprocess.run(command, capture_output=True, text=True, cwd=REPO)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("sustained candidate requires a fake-kernel test build", failed.stderr)

    def test_fake_root_cannot_be_enabled_without_test_build(self):
        (REPO / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="broker-guard-", dir=REPO / "build") as directory:
            command = ["cc", "-std=c11", '-DBROKER_TEST_ROOT="/tmp/unsafe"',
                       str(REPO / "broker" / "main.c"), "-o", str(pathlib.Path(directory) / "broker")]
            failed = subprocess.run(command, capture_output=True, text=True, cwd=REPO)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("fake sysfs root is only available in a test build", failed.stderr)

    def test_production_binary_has_no_fake_sysfs_path(self):
        (REPO / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="broker-prod-bin-", dir=REPO / "build") as directory:
            binary = pathlib.Path(directory) / "broker"
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            str(REPO / "broker" / "main.c"), "-o", str(binary)],
                           check=True, cwd=REPO)
            content = binary.read_bytes()
            self.assertIn(b"/sys/bus/platform/devices/omen-rgb", content)
            self.assertIn(b"/run/okeylitctl.sock", content)
            self.assertNotIn(b"BROKER_TEST_ROOT", content)
            self.assertNotIn(b"/tmp/broker-test-", content)
            self.assertNotIn(b"OK BURST\n", content)
            result = subprocess.run([str(binary)], capture_output=True, timeout=2)
            self.assertNotEqual(result.returncode, 0, "without systemd activation it must fail")

    def test_installer_units_define_non_world_socket_and_single_root_service(self):
        socket_unit = (REPO / "packaging" / "okeylitctl-broker.socket").read_text()
        service_unit = (REPO / "packaging" / "okeylitctl-broker.service").read_text()
        self.assertIn("ListenSequentialPacket=/run/okeylitctl.sock", socket_unit)
        self.assertIn("SocketMode=0660", socket_unit)
        self.assertIn("SocketGroup=okeylitctl", socket_unit)
        self.assertIn("Accept=no", socket_unit)
        self.assertIn("User=root", service_unit)
        self.assertIn("ExecStart=/usr/local/libexec/okeylitctl-broker", service_unit)


if __name__ == "__main__":
    unittest.main()
