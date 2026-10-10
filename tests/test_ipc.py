"""Device-free integration tests for the bounded broker client."""
import errno
import socket
import math
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from okeylitctl.ipc import ConflictError, IPCBackend
from okeylitctl.sysfs import BackendIOError, ModuleUnavailable, PermissionDenied, UnsupportedABI
from okeylitctl.validation import ValidationError


class BrokerFixture:
    def __init__(self, path, reply):
        self.path = path
        self.reply = reply
        self.requests = []
        self.error = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            with self.listener.accept()[0] as conn:
                self.requests.append(conn.recv(129))
                if self.reply is not None:
                    conn.sendall(self.reply)
                else:
                    time.sleep(0.25)
        except Exception as exc:
            self.error = exc

    def close(self):
        self.thread.join(2)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("broker client did not finish")
        if self.error:
            raise self.error


class IPCTests(unittest.TestCase):
    def exchange(self, operation, reply):
        with tempfile.TemporaryDirectory() as directory:
            fixture = BrokerFixture(Path(directory) / "broker.sock", reply)
            try:
                result = operation(IPCBackend(socket_path=fixture.path, timeout=0.1))
            finally:
                fixture.close()
            return result, fixture.requests

    def test_snapshot_uses_v2_packet_and_returns_token_plus_status(self):
        result, requests = self.exchange(
            lambda backend: backend.snapshot(),
            b"OK 0123456789ABCDEF:FEDCBA9876543210 off 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n",
        )
        self.assertEqual(requests, [b"2 SNAPSHOT\n"])
        self.assertEqual(result, {
            "token": "0123456789ABCDEF:FEDCBA9876543210",
            "state": "off",
            "colors": ["111111", "222222", "333333", "444444"],
            "original": ["AAAAAA", "BBBBBB", "CCCCCC", "DDDDDD"],
        })

    def test_cas_sends_expected_and_desired_wire_and_returns_new_token(self):
        token = "0123456789ABCDEF:FEDCBA9876543210"
        new_token = "1111111111111111:2222222222222222"
        expected = "111111,222222,333333,444444"
        desired = "ABCDEF,222222,333333,444444"
        result, requests = self.exchange(
            lambda backend: backend.compare_and_write(token, "off", expected, desired),
            f"OK {new_token}\n".encode(),
        )
        self.assertEqual(result, new_token)
        self.assertEqual(requests, [f"2 CAS {token} off {expected} {desired}\n".encode()])

    def test_snapshot_rejects_malformed_or_oversized_packets(self):
        valid = b"OK 0123456789ABCDEF:FEDCBA9876543210 on 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n"
        for reply in (b"", b"OK\n", valid[:-1], valid + b"EXTRA",
                      valid.replace(b"0123456789ABCDEF", b"0123456789abcdef", 1),
                      valid.replace(b"on", b"ON", 1),
                      valid.replace(b"111111", b"11111g", 1),
                      b"X" * 129, b"ERR CONFLICT\n"):
            with self.subTest(reply=reply), self.assertRaises(UnsupportedABI):
                self.exchange(lambda backend: backend.snapshot(), reply)

    def test_cas_conflict_is_definite_and_other_failures_are_not(self):
        args = ("0123456789ABCDEF:FEDCBA9876543210", "on",
                "111111,222222,333333,444444", "AAAAAA,222222,333333,444444")
        with self.assertRaises(ConflictError) as caught:
            _, _ = self.exchange(lambda backend: backend.compare_and_write(*args),
                                 b"ERR CONFLICT\n")
        self.assertNotIn("uncertain", str(caught.exception))
        for reply, error in ((b"ERR IO\n", BackendIOError),
                             (b"ERR ABI\n", UnsupportedABI),
                             (b"OK\n", UnsupportedABI),
                             (b"OK 0123456789abcdef:FEDCBA9876543210\n", UnsupportedABI),
                             (b"X" * 129, UnsupportedABI)):
            with self.subTest(reply=reply), self.assertRaises(error) as caught:
                self.exchange(lambda backend: backend.compare_and_write(*args), reply)
            self.assertNotIsInstance(caught.exception, ConflictError)
            self.assertIn("outcome uncertain", str(caught.exception))

    def test_cas_validates_all_fields_before_connecting(self):
        backend = IPCBackend(socket_path="/nonexistent/okeylitctl.sock")
        args = ("0123456789ABCDEF:FEDCBA9876543210", "on",
                "111111,222222,333333,444444", "AAAAAA,222222,333333,444444")
        for bad in (
            ("0123456789abcdef:FEDCBA9876543210", *args[1:]),
            ("0" * 33, *args[1:]),
            (args[0], "ON", *args[2:]),
            (*args[:2], "123456\n1 RESTORE", args[3]),
            (*args[:3], "ABCDEF,222222,333333,GGGGGG"),
        ):
            with self.subTest(args=bad), self.assertRaises(ValidationError):
                backend.compare_and_write(*bad)

    def test_status_uses_one_seqpacket_and_returns_existing_status_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(
                path,
                b"OK on FF0000,00FF00,0000FF,FFFFFF 580BC3,D00FEF,4D0998,AF0AA1\n",
            )
            try:
                result = IPCBackend(socket_path=path).status()
            finally:
                fixture.close()
            self.assertEqual(fixture.requests, [b"1 STATUS\n"])
            self.assertEqual(result, {
                "state": "on",
                "colors": ["FF0000", "00FF00", "0000FF", "FFFFFF"],
                "original": ["580BC3", "D00FEF", "4D0998", "AF0AA1"],
            })

    def test_write_colors_sends_one_canonical_packet(self):
        result, requests = self.exchange(
            lambda backend: backend.write_colors("abcdef,123456,000000,FFFFFF"), b"OK\n"
        )
        self.assertIsNone(result)
        self.assertEqual(requests, [b"1 SET ABCDEF,123456,000000,FFFFFF\n"])

    def test_restore_sends_one_packet(self):
        result, requests = self.exchange(lambda backend: backend.restore(), b"OK\n")
        self.assertIsNone(result)
        self.assertEqual(requests, [b"1 RESTORE\n"])

    def test_invalid_color_rejected_before_connect(self):
        with self.assertRaises(ValidationError):
            IPCBackend(socket_path="/nonexistent/okeylitctl.sock").write_colors("123456\n1 RESTORE")

    def test_timeout_must_be_positive_and_finite(self):
        for timeout in (math.nan, math.inf, -math.inf, 0, -1, "1", True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                IPCBackend(timeout=timeout)
        self.assertEqual(IPCBackend(timeout=0.05).timeout, 0.05)

    def test_error_codes_map_to_existing_exit_code_exceptions(self):
        cases = {
            b"ERR INVALID\n": ValidationError,
            b"ERR BUSY\n": BackendIOError,
            b"ERR MODULE\n": ModuleUnavailable,
            b"ERR DENIED\n": PermissionDenied,
            b"ERR IO\n": BackendIOError,
            b"ERR ABI\n": UnsupportedABI,
        }
        for reply, exception in cases.items():
            with self.subTest(reply=reply):
                with self.assertRaises(exception):
                    self.exchange(lambda backend: backend.restore(), reply)

    def test_post_write_broker_errors_warn_outcome_uncertain_without_retry(self):
        for reply, exception in ((b"ERR IO\n", BackendIOError), (b"ERR ABI\n", UnsupportedABI)):
            for operation, expected in (
                (lambda backend: backend.restore(), b"1 RESTORE\n"),
                (lambda backend: backend.write_colors("000000,000000,000000,000000"),
                 b"1 SET 000000,000000,000000,000000\n"),
            ):
                with self.subTest(reply=reply, request=expected), tempfile.TemporaryDirectory() as directory:
                    fixture = BrokerFixture(Path(directory) / "broker.sock", reply)
                    try:
                        with self.assertRaises(exception) as caught:
                            operation(IPCBackend(socket_path=fixture.path))
                    finally:
                        fixture.close()
                    self.assertIn("outcome uncertain", str(caught.exception).lower())
                    self.assertIn("do not retry", str(caught.exception).lower())
                    self.assertEqual(fixture.requests, [expected])

    def test_malformed_status_packets_fail_closed_as_abi_error(self):
        valid = b"OK on 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n"
        bad_packets = [
            b"", b"OK\n", b"OK yes 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n",
            valid[:-1], valid + b"\n", valid + b"EXTRA", valid.replace(b"on", b"ON", 1),
            valid.replace(b"111111", b"gggggg", 1), valid.replace(b"AAAAAA", b"aaaaaa", 1),
            valid.replace(b" ", b"  ", 1), b"OK \xff\n", b"ERR WHATEVER\n",
            b"X" * 129, b"OK\x00\n", b"ERR IO\nOK\n",
        ]
        for reply in bad_packets:
            with self.subTest(reply=reply):
                with self.assertRaises(UnsupportedABI):
                    self.exchange(lambda backend: backend.status(), reply)

    def test_mutation_ack_requires_exact_ok_packet(self):
        for reply in (b"", b"OK", b"OK \n", b"OK on 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n", b"OK\nOK\n", b"X" * 129):
            with self.subTest(reply=reply):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BrokerFixture(Path(directory) / "broker.sock", reply)
                    try:
                        with self.assertRaises(UnsupportedABI) as caught:
                            IPCBackend(socket_path=fixture.path).restore()
                    finally:
                        fixture.close()
                    self.assertIn("outcome uncertain", str(caught.exception).lower())
                    self.assertIn("do not retry", str(caught.exception).lower())
                    self.assertEqual(fixture.requests, [b"1 RESTORE\n"])

    def test_missing_broker_is_io_error_not_kernel_module_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BackendIOError):
                IPCBackend(socket_path=Path(directory) / "absent.sock").status()

    def test_symlink_to_socket_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            link = Path(directory) / "alias.sock"
            fixture = BrokerFixture(path, b"OK\n")
            link.symlink_to(path)
            try:
                with self.assertRaises(BackendIOError):
                    IPCBackend(socket_path=link).restore()
                self.assertEqual(fixture.requests, [])
            finally:
                fixture.listener.close()
                # No connection was made; the fixture thread is intentionally a daemon.

    def test_symlinked_or_writable_parent_is_refused_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            private.mkdir()
            path = private / "broker.sock"
            fixture = BrokerFixture(path, b"OK\n")
            alias = Path(directory) / "alias"
            alias.symlink_to(private, target_is_directory=True)
            try:
                with self.assertRaises(BackendIOError):
                    IPCBackend(socket_path=alias / "broker.sock").restore()
                private.chmod(0o777)
                with self.assertRaises(BackendIOError):
                    IPCBackend(socket_path=path).restore()
                self.assertEqual(fixture.requests, [])
            finally:
                private.chmod(0o700)
                fixture.listener.close()  # Neither call connects.

    def test_path_replacement_during_connect_is_refused_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            replacement = Path(directory) / "replacement.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            listener.bind(str(path))
            listener.listen(1)
            real_socket = socket.socket

            class SwappingSocket:
                def __init__(self, *args):
                    self.conn = real_socket(*args)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.conn.close()

                def settimeout(self, value):
                    self.conn.settimeout(value)

                def connect(self, address):
                    os.replace(path, replacement)
                    self.conn.connect(str(replacement))

                def send(self, packet):
                    raise AssertionError("must not send to a replaced socket")

            try:
                with patch("okeylitctl.ipc.socket.socket", SwappingSocket):
                    with self.assertRaises(BackendIOError):
                        IPCBackend(socket_path=path).restore()
            finally:
                listener.close()

    def test_wrong_peer_uid_is_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(path, None)
            real_socket = socket.socket

            class WrongPeerSocket:
                def __init__(self, *args):
                    self.conn = real_socket(*args)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.conn.close()

                def settimeout(self, value):
                    self.conn.settimeout(value)

                def connect(self, address):
                    self.conn.connect(address)

                def getsockopt(self, *args):
                    return struct.pack("3i", os.getpid(), os.geteuid() + 1, os.getegid())

                def send(self, packet):
                    raise AssertionError("must not send to an untrusted peer")

            try:
                with patch("okeylitctl.ipc.socket.socket", side_effect=lambda *args, **kw:
                           real_socket(*args, **kw) if "fileno" in kw else WrongPeerSocket(*args, **kw)):
                    with self.assertRaises(BackendIOError):
                        IPCBackend(socket_path=path).restore()
            finally:
                fixture.close()
            self.assertEqual(fixture.requests, [b""])

    def test_send_error_after_broker_received_request_is_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(path, None)
            real_socket = socket.socket

            class AmbiguousSend:
                def __init__(self, *args):
                    self.conn = real_socket(*args)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.conn.close()

                def settimeout(self, value):
                    self.conn.settimeout(value)

                def connect(self, address):
                    self.conn.connect(address)

                def getsockopt(self, *args):
                    return self.conn.getsockopt(*args)

                def send(self, packet):
                    self.conn.send(packet)
                    raise OSError(errno.EIO, "ambiguous send")

            try:
                with patch("okeylitctl.ipc.socket.socket", side_effect=lambda *args, **kw:
                           real_socket(*args, **kw) if "fileno" in kw else AmbiguousSend(*args, **kw)):
                    with self.assertRaises(BackendIOError) as caught:
                        IPCBackend(socket_path=path).restore()
            finally:
                fixture.close()
            self.assertIn("outcome uncertain", str(caught.exception).lower())
            self.assertEqual(fixture.requests, [b"1 RESTORE\n"])

    def test_timeout_on_write_response_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = BrokerFixture(Path(directory) / "broker.sock", None)
            try:
                with self.assertRaises(BackendIOError) as caught:
                    IPCBackend(socket_path=fixture.path, timeout=0.05).write_colors(
                        "000000,000000,000000,000000"
                    )
            finally:
                fixture.close()
        self.assertIn("uncertain", str(caught.exception).lower())
        self.assertEqual(fixture.requests, [b"1 SET 000000,000000,000000,000000\n"])


if __name__ == "__main__":
    unittest.main()
