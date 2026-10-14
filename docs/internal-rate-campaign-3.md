# Fresh bounded 4/5 Hz campaign (internal; not a supported rate)

Campaign 3 uses `/var/lib/okeylitctl-rate-experiment-3`, a separate
root-owned StateDirectory, lease stamp, stage, and receipt. Never delete or
reset `/var/lib/okeylitctl-rate-experiment` **or**
`/var/lib/okeylitctl-rate-experiment-2` or their durable markers. The
ordinary broker and unit remain the manifest-backed installation; the campaign only temporarily swaps those two files and backs
them up byte-for-byte. The normal build/installer never enables BURST.

Only stage a clean, reviewed exact feature HEAD after passing device-free tests
and CI. The administrator explicitly trusts the local checkout when copying
into the root-only stage; do not run the checkout's Python script as root.
Build `make broker broker-rate-campaign-3`. Stage the campaign-3 binary,
`packaging/okeylitctl-broker-rate-campaign-3.service`, and
`scripts/rate-campaign-3-deploy.py` in the **new** StateDirectory's 0700
`stage` directory, with modes 0755, 0644, 0644. Use the staged tool's `deploy`
action with reviewed artifact hashes and the current ordinary binary/service
hashes; the tool verifies the installed ordinary pair against the root-owned
manifest before stopping the socket. Do not touch either prior StateDirectory.

After installation, check active socket and unprivileged status, record the
base colors and power. One foreground TUI Start at 4 Hz consumes one durable
three-second lease (at most ten frames); Stop and independently read back the
base before any advancement. Only when 4 Hz restoration is verified, use the
**staged campaign-3 tool** `rearm` once, then repeat at 5 Hz. An uncertain
write/Stop or ownership loss blocks rearm and all further writes. On successful
baseline verification use the staged tool `restore`, then verify the ordinary
binary/unit hashes, active socket, and unprivileged status. Restore does not
remove the durable campaign-3 `attempted`, `rearms`, or `used` markers.

A service restart never grants a second lease. During the three-second lease,
the provisional rolling ceiling is 49 firmware-facing reads/s, 7 writes/s;
ordinary ceilings resume afterward. These are *accounting limits*, not
hardware-safe-rate or thermal claims. Fake-kernel tests cannot establish how
smooth, responsive, or thermally safe the physical keyboard is. Record actual
completed-write spacing, I/O and Stop latency, readback, perceived motion,
and thermals separately. Do not merge, release, or advertise 4/5 Hz support on
simulated evidence.
