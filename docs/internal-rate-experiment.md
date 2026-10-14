# Internal, unvalidated broker rate experiment — NOT an installation guide

No laptop use until an independent reviewer approves the exact diagnostic commit,
the full diff is committed/published on the feature branch, exact-head CI passes,
and the owner explicitly authorizes each hardware checkpoint. These commands are
stages for a future one-stage-at-a-time session, **not commands to run now**.
The diagnostic does not establish a safe permanent rate or thermal behavior.
Never use `sudo` on a user-writable checkout script. No driver reinstall is needed.

Review record required before any staging: full 40-character commit SHA (`HEAD`),
SHA-256 of the built diagnostic binary, ordinary binary, diagnostic unit, deployment
script, and the installed ordinary service unit. No values can be finalized while
this change remains uncommitted. Compare each hash with the approved review record;
never populate one from an unexpected local build or changed installation.

1. Unprivileged checkout preflight: `git status --porcelain=v1 --untracked-files=all`,
   `git rev-parse HEAD`, `git rev-parse origin/feature/rootless-backend`. Require
   an empty status and equality to the reviewed exact SHA and published PR head.
   Inspect ignored inputs as well; do not build from an unexpected file.
2. Unprivileged build: `make broker broker-rate-experiment` and `sha256sum
   build/okeylitctl-broker build/okeylitctl-broker-rate-experiment
   packaging/okeylitctl-broker.service scripts/rate-experiment-deploy.py`.
   Compare against independently reviewed artifacts. On the laptop, the installed
   ordinary broker/service hashes must match the root-owned DKMS manifest's eighth
   and ninth lines (the deployment tool checks this before stopping the socket).
3. Explicit privileged staging, only after the trusted-checkout boundary has been
   accepted: `sudo install -d -m 0700 /var/lib/okeylitctl-rate-experiment` and
   separately `sudo install -d -m 0700
   /var/lib/okeylitctl-rate-experiment/stage`. Copy the reviewed binary with
   `sudo install -m 0755 build/okeylitctl-broker-rate-experiment
   /var/lib/okeylitctl-rate-experiment/stage/diagnostic`, the reviewed service
   with `sudo install -m 0644 packaging/okeylitctl-broker.service
   /var/lib/okeylitctl-rate-experiment/stage/diagnostic.service`, and the reviewed
   tool with `sudo install -m 0644 scripts/rate-experiment-deploy.py
   /var/lib/okeylitctl-rate-experiment/stage/deploy.py`. Verify SHA-256 on **staged**
   files against the approved record before using the tool. If other users can
   alter checkout ancestors, stage only from a root-owned immutable reviewed archive.
4. Stop any effect and verify ordinary, unprivileged `okeylitctl status` before
   deploying. `sudo python3 -I
   /var/lib/okeylitctl-rate-experiment/stage/deploy.py deploy
   /var/lib/okeylitctl-rate-experiment/stage/diagnostic <reviewed-diagnostic-sha256>
   /var/lib/okeylitctl-rate-experiment/stage/diagnostic.service
   <reviewed-diagnostic-service-sha256> <installed-ordinary-broker-sha256>
   <installed-ordinary-service-sha256>` is one bounded stage: it stops socket and
   service, stores an owner-only byte-exact ordinary backup and receipt, atomically
   replaces binary/unit, reloads systemd, starts socket, and rolls back on a
   normal deployment error. A durable attempt marker blocks a second deployment,
   including after an interrupted pre-receipt backup; if backup fails before the
   receipt, the unchanged ordinary binary/unit are verified and the socket is
   restarted. If a machine crash or failed rollback interrupts it, run `restore`
   only after checking receipt and installed hashes; without a receipt, stop and
   inspect partial backups and ordinary installed hashes for manual recovery.
5. Check `systemctl is-active okeylitctl-broker.socket` and unprivileged
   `okeylitctl status` (power on, original and colors recorded). Run
   `okeylitctl tui --test-rate-hz 4 --rate-experiment`, choose a named effect,
   deliberately Start, observe at most ten frames / three seconds, Stop, and
   check unprivileged `okeylitctl status` against the recorded base. Record
   completed I/O spacing, readback, Stop latency, motion, and thermals. A failed
   or uncertain Stop blocks any next rate or broker swap until reconciled.
6. Only after verified 4 Hz restoration: `sudo python3 -I
   /var/lib/okeylitctl-rate-experiment/stage/deploy.py rearm` durably records
   the sole additional lease before stopping both units, removes
   only the root-owned consumed stamp, then restarts the socket. The rearm count
   survives failure and reboot; repeated rearm is refused even if the used stamp
   reappears. A service restart alone cannot rearm. Recheck status, then run
   `okeylitctl tui --test-rate-hz 5 --rate-experiment` with the same guarded
   Start/Stop/readback criteria. Do not rearm repeatedly or advance on failure.
7. After verified base restoration: `sudo python3 -I
   /var/lib/okeylitctl-rate-experiment/stage/deploy.py restore`. Verify socket
   active, unprivileged `okeylitctl status`, and hashes of both restored files
   against manifest lines eight/nine. The consumed stamp remains to prevent
   accidental reactivation; the attempt and rearm markers also remain. Ordinary
   broker rejects `BURST` altogether. Do not
   uninstall or run installer while the temporary diagnostic differs from the
   installer manifest. Retain the root-only staging and receipt on any recovery
   failure; escalate instead of attempting an uncertain write or blind rollback.

`deploy`, `rearm`, and `restore` are each separate consequential checkpoints. The
operator must inspect full output/readback and authorize the next stage; do not
paste or execute the entire sequence as a batch. The root-owned tool and stamp
are deliberately outside the ordinary broker's user-accessible socket protocol.
