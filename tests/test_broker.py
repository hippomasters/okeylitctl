"""Device-free protocol and security integration tests for the native broker."""
import os
import pathlib
import re
import shlex
import socket
import subprocess
import tempfile
import time
import unittest

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
#include <unistd.h>
#ifndef BROKER_TEST_ROOT
#error "fake kernel must be built for the fake root"
#endif
ssize_t write(int fd, const void *buf, size_t len) {
    ssize_t n = syscall(SYS_write, fd, buf, len);
    const char *mode = getenv("BROKER_FAKE_KERNEL");
    if (!mode || n != (ssize_t)len) return n;
    char link[64], path[512];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t size = readlink(link, path, sizeof(path) - 1);
    if (size < 0) return n;
    path[size] = 0;
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

    def launch_broker(self, fake_mode=None):
        fd = self.listener.fileno()
        command = f"exec 3<&{fd}; LISTEN_PID=$$ exec {shlex.quote(str(self.binary))}"
        env = dict(os.environ, LISTEN_FDS="1")
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

    def restart_with_fake_kernel(self, mode):
        self.proc.terminate()
        self.proc.communicate(timeout=2)
        self.launch_broker(mode)

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

    def test_five_hz_snapshots_and_cas_sustain_verified_updates(self):
        start = time.monotonic()
        expected, desired = BASE, ALT
        for step in range(6):
            target = start + step * 0.21
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
            token, state, colors, _ = self.snapshot()
            self.assertEqual((state, colors), (b"on", expected))
            self.assertRegex(self.cas(token, expected=expected, desired=desired),
                             rb"\AOK [0-9A-F]{16}:[0-9A-F]{16}\n\Z")
            expected, desired = desired, expected
        self.assertEqual((self.root / "colors").read_bytes(), expected + b"\n")

    def test_five_hz_legacy_status_and_cas_keep_readback_budget(self):
        expected, desired = BASE, ALT
        token = self.snapshot()[0]
        start = time.monotonic()
        for step in range(6):
            target = start + (step + 1) * 0.21
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

    def test_off_state_cas_and_uncertain_cas_invalidate_token(self):
        self.put("state", b"off\n")
        token, state, _, _ = self.snapshot()
        self.assertEqual(state, b"off")
        self.assertTrue(self.cas(token, state=b"off").startswith(b"OK "))
        self.restart_with_fake_kernel("mismatch")
        token = self.snapshot()[0]
        self.assertEqual(self.cas(token, state=b"off", expected=ALT, desired=ALT),
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

    def test_five_hz_status_and_set_sustain_verified_updates(self):
        start = time.monotonic()
        for step in range(6):
            target = start + step * 0.21
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


class BrokerPackaging(unittest.TestCase):
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
