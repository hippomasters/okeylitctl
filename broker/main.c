/* Narrow, socket-activated privileged bridge for the omen-rgb sysfs ABI.
 * No dynamic paths, profiles, subprocesses or firmware commands from clients.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/magic.h>
#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#if defined(BROKER_TEST_ROOT) && !defined(BROKER_TESTING)
#error "fake sysfs root is only available in a test build"
#endif
#if defined(BROKER_TEST_SOCKET) && !defined(BROKER_TESTING)
#error "fake socket name is only available in a test build"
#endif
#ifdef BROKER_TESTING
#ifndef BROKER_TEST_ROOT
#error "test build needs an explicit fake root"
#endif
#ifndef BROKER_TEST_SOCKET
#error "test build needs an explicit fake socket name"
#endif
#define DEVICE_ROOT BROKER_TEST_ROOT
#define SOCKET_PATH BROKER_TEST_SOCKET
#else
#define DEVICE_ROOT "/sys/bus/platform/devices/omen-rgb"
#define SOCKET_PATH "/run/okeylitctl.sock"
#endif

#define PACKET_MAX 128
#define READ_MAX 128
#define REQUEST_MS 1000
#define PENDING_MAX 16
#define WRITE_LIMIT 5
/* Provisional WMI GET ceiling, not a measured hardware-safe maximum.
 * SNAPSHOT: colors + state = 2 GETs (original and ABI are cached sysfs).
 * CAS: state + colors + kernel pre-read + kernel verification + broker
 * readback = 5 GETs. A complete snapshot/CAS frame costs 7 GETs.
 * Without an explicit lease, 4/5 Hz cannot fit this rolling ceiling.
 */
#define READ_LIMIT 25
#define STATUS_READ_LIMIT 10 /* read-only clients cannot starve writes */
#define CAS_READ_LIMIT 20
#define WINDOW_MS 1000
#define WIRE_LEN 27
/* Provisional rate policy: the normal build elevates quotas for one three-second
 * persistent-stamp-bounded lease; the separate opt-in long build permits only
 * 4 Hz for at most 15 seconds. The unrestricted candidate is fake-kernel-only.
 * Outside a lease, ordinary traffic retains the 25 GET ceiling. Each verified
 * snapshot/CAS frame costs seven WMI GETs. Keep one complete Stop snapshot/CAS
 * restoration (2+5 GETs and one SET) outside ordinary status, CAS and write
 * admission. These quotas are not measured physical safety claims. */
#if defined(BROKER_SUSTAINED_CANDIDATE) && !defined(BROKER_TESTING)
#error "sustained candidate requires a fake-kernel test build"
#endif
#if defined(BROKER_LONG_4HZ_DIAGNOSTIC) && (defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_RATE_EXPERIMENT) || defined(BROKER_RATE_CAMPAIGN_2) || defined(BROKER_RATE_CAMPAIGN_3))
#error "long diagnostic must be a separate opt-in broker policy"
#endif
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
#define BROKER_RATE_EXPERIMENT
#endif
#if defined(BROKER_SUSTAINED_CANDIDATE) && defined(BROKER_RATE_EXPERIMENT)
#error "sustained candidate and diagnostic lease are mutually exclusive"
#endif
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
#define CANDIDATE_READ_LIMIT 49
#define CANDIDATE_STATUS_READ_LIMIT 16
#define CANDIDATE_CAS_READ_LIMIT 30
#define CANDIDATE_WRITE_LIMIT 6
#define CANDIDATE_STATUS_ORDINARY 14 /* ordinary snapshots cannot consume two Stop slots */
#define CANDIDATE_CAS_ORDINARY 25
#define CANDIDATE_WRITE_ORDINARY 5
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
#define LONG_WRITE_ORDINARY 4 /* Stop keeps separate write admission. */
#define LONG_FRAME_INTERVAL_MS 250
#endif
#endif

/* Diagnostic binaries are not built or installed by the normal broker target.
 * A distinct root-owned persistent stamp allows one lease (three seconds for
 * prior campaigns, 15 seconds for the long 4 Hz diagnostic) across restarts.
 * Rearm needs separate administrator review with the service/socket stopped.
 * All GETs stay prebooked and verified; after expiry, Stop uses ordinary quotas
 * and bounded waiting rather than bypassing safety limits. */
#if defined(BROKER_RATE_CAMPAIGN_2) && defined(BROKER_RATE_CAMPAIGN_3)
#error "diagnostic campaigns must be mutually exclusive"
#endif
#if (defined(BROKER_RATE_CAMPAIGN_2) || defined(BROKER_RATE_CAMPAIGN_3)) && !defined(BROKER_RATE_EXPERIMENT)
#error "diagnostic campaign requires the bounded rate experiment"
#endif
#ifdef BROKER_RATE_EXPERIMENT
#define MAX_READ_LIMIT 49
#define MAX_STATUS_READ_LIMIT 16
#define MAX_CAS_READ_LIMIT 35
#define MAX_WRITE_LIMIT 7
static int64_t burst_until;
static bool burst_used;
#ifdef BROKER_TESTING
#define BURST_DIRECTORY DEVICE_ROOT
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
#define BURST_STAMP "long-4hz-diagnostic-used"
#elif defined(BROKER_BOUNDED_NORMAL)
#define BURST_STAMP "normal-bounded-used"
#elif defined(BROKER_RATE_CAMPAIGN_3)
#define BURST_STAMP "rate-experiment-3-used"
#elif defined(BROKER_RATE_CAMPAIGN_2)
#define BURST_STAMP "rate-experiment-2-used"
#else
#define BURST_STAMP "rate-experiment-used"
#endif
#else
/* StateDirectory in the service persists across broker restarts/reboots.
 * Only an administrator may remove this stamp between separately reviewed rates. */
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
#define BURST_DIRECTORY "/var/lib/okeylitctl-long-4hz-diagnostic"
#elif defined(BROKER_BOUNDED_NORMAL)
#define BURST_DIRECTORY "/var/lib/okeylitctl-rate-experiment"
#elif defined(BROKER_RATE_CAMPAIGN_3)
#define BURST_DIRECTORY "/var/lib/okeylitctl-rate-experiment-3"
#elif defined(BROKER_RATE_CAMPAIGN_2)
#define BURST_DIRECTORY "/var/lib/okeylitctl-rate-experiment-2"
#else
#define BURST_DIRECTORY "/var/lib/okeylitctl-rate-experiment"
#endif
#ifdef BROKER_BOUNDED_NORMAL
#define BURST_STAMP "normal-bounded-used"
#else
#define BURST_STAMP "used"
#endif
#endif
static bool burst_active(int64_t current)
{
    return burst_used && current >= 0 && current < burst_until;
}
#elif defined(BROKER_SUSTAINED_CANDIDATE)
#define MAX_READ_LIMIT CANDIDATE_READ_LIMIT
#define MAX_STATUS_READ_LIMIT CANDIDATE_STATUS_READ_LIMIT
#define MAX_CAS_READ_LIMIT CANDIDATE_CAS_READ_LIMIT
#define MAX_WRITE_LIMIT CANDIDATE_WRITE_LIMIT
#else
#define MAX_READ_LIMIT READ_LIMIT
#define MAX_STATUS_READ_LIMIT STATUS_READ_LIMIT
#define MAX_CAS_READ_LIMIT CAS_READ_LIMIT
#define MAX_WRITE_LIMIT WRITE_LIMIT
#endif

static size_t read_limit(int64_t current)
{
    (void)current;
#ifdef BROKER_RATE_EXPERIMENT
    if (burst_active(current)) return MAX_READ_LIMIT;
#elif defined(BROKER_SUSTAINED_CANDIDATE)
    return CANDIDATE_READ_LIMIT;
#endif
    return READ_LIMIT;
}
static size_t status_limit(int64_t current)
{
    (void)current;
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
    if (burst_active(current)) return CANDIDATE_STATUS_READ_LIMIT;
#elif defined(BROKER_RATE_EXPERIMENT)
    if (burst_active(current)) return MAX_STATUS_READ_LIMIT;
#elif defined(BROKER_SUSTAINED_CANDIDATE)
    return CANDIDATE_STATUS_READ_LIMIT;
#endif
    return STATUS_READ_LIMIT;
}
static size_t cas_limit(int64_t current)
{
    (void)current;
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
    if (burst_active(current)) return CANDIDATE_CAS_READ_LIMIT;
#elif defined(BROKER_RATE_EXPERIMENT)
    if (burst_active(current)) return MAX_CAS_READ_LIMIT;
#elif defined(BROKER_SUSTAINED_CANDIDATE)
    return CANDIDATE_CAS_READ_LIMIT;
#endif
    return CAS_READ_LIMIT;
}
static size_t write_limit(int64_t current)
{
    (void)current;
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
    if (burst_active(current)) return CANDIDATE_WRITE_LIMIT;
#elif defined(BROKER_RATE_EXPERIMENT)
    if (burst_active(current)) return MAX_WRITE_LIMIT;
#elif defined(BROKER_SUSTAINED_CANDIDATE)
    return CANDIDATE_WRITE_LIMIT;
#endif
    return WRITE_LIMIT;
}

enum result { OK, INVALID, BUSY, MODULE, DENIED, IO, ABI, CONFLICT };
enum read_kind { SNAPSHOT_READ, CAS_READ };
enum attribute { ABI_VERSION, COLORS, ORIGINAL, STATE, RESTORE };
static const char *const attributes[] = {
    "abi_version", "colors", "original", "state", "restore"
};
static const mode_t attribute_modes[] = { 0444, 0600, 0444, 0400, 0200 };
static int64_t write_times[MAX_WRITE_LIMIT];
static size_t write_count;
static int64_t read_times[MAX_READ_LIMIT];
static size_t read_count;
static int64_t status_times[MAX_STATUS_READ_LIMIT];
static size_t status_count;
static int64_t cas_times[MAX_CAS_READ_LIMIT];
static size_t cas_count;
/* The broker is single-threaded. Keep each prebooked operation's slots until
 * its last sysfs read has completed, not only until one second after admission:
 * otherwise late WMI GETs can overlap a new window and exceed the ceiling. */
static size_t booked_reads, booked_read_offset, booked_kind_offset;
static enum read_kind booked_kind;
static bool booked_write;
static size_t booked_write_offset;
static uint64_t nonce, sequence;
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
static bool stop_token_ready;
#endif

static int64_t now_ms(void);
#ifdef BROKER_RATE_EXPERIMENT
static enum result consume_burst_stamp(void)
{
    int dir = open(BURST_DIRECTORY, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dir < 0) return DENIED;
    struct stat st;
    enum result result = DENIED;
    if (fstat(dir, &st) != 0 || !S_ISDIR(st.st_mode) ||
        st.st_uid != geteuid() || (st.st_mode & 0022)) goto done;
    int fd = openat(dir, BURST_STAMP,
                    O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) { result = errno == EEXIST ? BUSY : DENIED; goto done; }
    /* Existence alone consumes the lease; never remove it on a later failure. */
    close(fd);
    result = OK;
done:
    close(dir);
    return result;
}
#endif
static void complete_bookings(void)
{
    int64_t completed = now_ms();
    if (completed < 0) return; /* retain conservative original reservations */
    /* now_ms truncates sub-millisecond time. Ceil the completed reservation
     * so a new operation cannot start up to 999us before the last GET ages. */
    if (completed < INT64_MAX) ++completed;
    for (size_t i = 0; i < booked_reads; ++i) {
        read_times[booked_read_offset + i] = completed;
        if (booked_kind == SNAPSHOT_READ)
            status_times[booked_kind_offset + i] = completed;
        else
            cas_times[booked_kind_offset + i] = completed;
    }
    if (booked_write)
        write_times[booked_write_offset] = completed;
}

static void token_string(char out[34])
{
    snprintf(out, 34, "%016" PRIX64 ":%016" PRIX64, nonce, sequence);
}

#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
/* A distinct Stop token grants reserved restoration capacity only to a
 * client that obtained a STOPSHOT of this exact broker revision. */
static void stop_token_string(char out[34])
{
    snprintf(out, 34, "%016" PRIX64 ":%016" PRIX64,
             nonce ^ UINT64_C(0x8000000000000000), sequence);
}
#endif

static bool token_valid(const char *s)
{
    for (size_t i = 0; i < 33; ++i) {
        if (i == 16) { if (s[i] != ':') return false; }
        else if (!((s[i] >= '0' && s[i] <= '9') ||
                   (s[i] >= 'A' && s[i] <= 'F'))) return false;
    }
    return true;
}

static int64_t now_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static enum result error_from_errno(int number)
{
    switch (number) {
    case ENOENT: case ENODEV: case ENOTDIR: return MODULE;
    case EACCES: case EPERM: case ELOOP: case EISDIR: return DENIED;
    case EAGAIN: case EBUSY: case ETIMEDOUT: return BUSY;
    default: return IO;
    }
}

static enum result check_deadline(int64_t deadline)
{
    int64_t current = now_ms();
    return current < 0 ? IO : (current >= deadline ? BUSY : OK);
}

static bool wire_valid(const char *s, size_t n)
{
    if (n != WIRE_LEN)
        return false;
    for (size_t i = 0; i < WIRE_LEN; ++i) {
        if (i == 6 || i == 13 || i == 20) {
            if (s[i] != ',') return false;
        } else if (!((s[i] >= '0' && s[i] <= '9') ||
                     (s[i] >= 'A' && s[i] <= 'F'))) {
            return false;
        }
    }
    return true;
}

static enum result reserve_slot(int64_t *times, size_t *count, size_t capacity)
{
    int64_t current = now_ms();
    if (current < 0) return IO;
    size_t retained = 0;
    for (size_t i = 0; i < *count; ++i)
        if (current - times[i] < WINDOW_MS)
            times[retained++] = times[i];
    *count = retained;
    /* A diagnostic lease can expire while more than the ordinary five
     * entries remain in the rolling window. Never admit or append then. */
    if (*count >= capacity) return BUSY;
    times[(*count)++] = current; /* failed attempts consume budget */
    return OK;
}

/* Reserve all firmware-facing GETs before starting each operation, including
 * the two GETs executed inside the kernel's colors_store/restore_store. */
static enum result reserve_reads(size_t count, enum read_kind kind, bool stop_priority)
{
    int64_t current = now_ms();
    if (current < 0) return IO;
    size_t retained = 0, status_retained = 0, cas_retained = 0;
    for (size_t i = 0; i < read_count; ++i)
        if (current - read_times[i] < WINDOW_MS)
            read_times[retained++] = read_times[i];
    for (size_t i = 0; i < status_count; ++i)
        if (current - status_times[i] < WINDOW_MS)
            status_times[status_retained++] = status_times[i];
    for (size_t i = 0; i < cas_count; ++i)
        if (current - cas_times[i] < WINDOW_MS)
            cas_times[cas_retained++] = cas_times[i];
    read_count = retained;
    status_count = status_retained;
    cas_count = cas_retained;
    size_t status_cap = status_limit(current), cas_cap = cas_limit(current);
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
    if (!stop_priority
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
        && burst_active(current)
#endif
    ) {
        status_cap = CANDIDATE_STATUS_ORDINARY;
        cas_cap = CANDIDATE_CAS_ORDINARY;
    }
#else
    (void)stop_priority;
#endif
    if (read_count + count > read_limit(current) ||
        (kind == SNAPSHOT_READ && status_count + count > status_cap) ||
        (kind == CAS_READ && cas_count + count > cas_cap)) return BUSY;
    booked_reads = count;
    booked_read_offset = read_count;
    booked_kind_offset = kind == SNAPSHOT_READ ? status_count : cas_count;
    booked_kind = kind;
    for (size_t i = 0; i < count; ++i) {
        read_times[read_count++] = current;
        if (kind == SNAPSHOT_READ) status_times[status_count++] = current;
        if (kind == CAS_READ) cas_times[cas_count++] = current;
    }
    return OK;
}

/* The fixed bus path is a kernel-owned symlink to its platform device.
 * Only its final directory link is followed; children are opened O_NOFOLLOW.
 * A production broker additionally requires a root-owned sysfs mount.
 */
static enum result open_root(int *root, struct stat *root_stat)
{
    *root = open(DEVICE_ROOT, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NONBLOCK);
    if (*root < 0)
        return error_from_errno(errno);
    if (fstat(*root, root_stat) < 0)
        return IO;
    if (!S_ISDIR(root_stat->st_mode) || (root_stat->st_mode & 0022))
        return DENIED;
#ifdef BROKER_TESTING
    if (root_stat->st_uid != geteuid()) return DENIED;
#else
    struct statfs fs;
    if (root_stat->st_uid != 0 || fstatfs(*root, &fs) < 0 ||
        (unsigned long)fs.f_type != SYSFS_MAGIC)
        return DENIED;
#endif
    return OK;
}

static enum result open_attribute(int root, const struct stat *dir_stat,
                                  enum attribute which, int flags, int *fd)
{
    *fd = openat(root, attributes[which], flags | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (*fd < 0)
        return error_from_errno(errno);
    struct stat info;
    if (fstat(*fd, &info) < 0)
        return IO;
    if (!S_ISREG(info.st_mode) || info.st_dev != dir_stat->st_dev ||
        info.st_uid != dir_stat->st_uid ||
        (info.st_mode & 0777) != attribute_modes[which] || info.st_nlink != 1)
        return DENIED;
    return OK;
}

static enum result read_attribute(int root, const struct stat *dir_stat,
                                  enum attribute which, char out[READ_MAX + 1],
                                  int64_t deadline)
{
    enum result result = check_deadline(deadline);
    if (result != OK) return result;
    int fd = -1;
    result = open_attribute(root, dir_stat, which, O_RDONLY, &fd);
    if (result == OK) {
        ssize_t n = read(fd, out, READ_MAX + 1);
        if (n < 0) result = error_from_errno(errno);
        else if (n == 0 || n > READ_MAX || out[n - 1] != '\n') result = ABI;
        else {
            /* Exactly one terminal LF; no other control bytes. */
            --n;
            if (n == 0) result = ABI;
            for (ssize_t i = 0; i < n && result == OK; ++i)
                if ((unsigned char)out[i] < 0x20 || (unsigned char)out[i] > 0x7e)
                    result = ABI;
            out[n] = '\0';
        }
    }
    if (fd >= 0) close(fd);
    if (result == OK) result = check_deadline(deadline);
    return result;
}

static enum result read_wire(int root, const struct stat *st, enum attribute which,
                             char out[READ_MAX + 1], int64_t deadline)
{
    enum result result = read_attribute(root, st, which, out, deadline);
    if (result == OK && !wire_valid(out, strlen(out))) return ABI;
    return result;
}

static enum result check_abi(int root, const struct stat *st, int64_t deadline)
{
    char version[READ_MAX + 1];
    enum result result = read_attribute(root, st, ABI_VERSION, version, deadline);
    return result == OK && strcmp(version, "1") != 0 ? ABI : result;
}

static enum result read_state(int root, const struct stat *st,
                              char out[READ_MAX + 1], int64_t deadline)
{
    enum result result = read_attribute(root, st, STATE, out, deadline);
    if (result == OK && strcmp(out, "on") && strcmp(out, "off")) return ABI;
    return result;
}

static enum result peer_present(int client)
{
    struct pollfd peer = { .fd = client, .events = POLLRDHUP };
    int ready = poll(&peer, 1, 0);
    if (ready < 0) return IO;
    return peer.revents & (POLLHUP | POLLRDHUP | POLLERR | POLLNVAL) ? BUSY : OK;
}

/* Stop reconciliation may wait for capacity, but never bypass a rolling limit. */
static enum result reserve_stop_reads(int64_t deadline, int client)
{
    for (;;) {
        enum result result = check_deadline(deadline);
        if (result != OK) return result;
        result = reserve_reads(2, SNAPSHOT_READ, true);
        if (result != BUSY) return result;
        int64_t current = now_ms();
        if (current < 0) return IO;
        int64_t next = deadline;
        if (read_count + 2 > read_limit(current) && read_count > 0 &&
            read_times[0] + WINDOW_MS < next)
            next = read_times[0] + WINDOW_MS;
        if (status_count + 2 > status_limit(current) && status_count > 0 &&
            status_times[0] + WINDOW_MS < next)
            next = status_times[0] + WINDOW_MS;
        int wait = (int)(next - current);
        if (wait < 1) wait = 1;
        struct pollfd peer = { .fd = client, .events = POLLRDHUP };
        if (poll(&peer, 1, wait) < 0 && errno != EINTR) return IO;
        if (peer.revents & (POLLHUP | POLLRDHUP | POLLERR | POLLNVAL)) return BUSY;
    }
}

/* Mutation admission waits only until the request deadline. The complete
 * kernel pre-read, kernel verification and broker readback are prebooked. */
static enum result reserve_mutation_reads(size_t count, int64_t deadline, int client,
                                          bool stop_priority)
{
    for (;;) {
        enum result result = check_deadline(deadline);
        if (result != OK) return result;
        result = reserve_reads(count, CAS_READ, stop_priority);
        if (result != BUSY) return result;
        int64_t current = now_ms();
        if (current < 0) return IO;
        int64_t next = deadline;
        if (read_count + count > read_limit(current) && read_count > 0 &&
            read_times[0] + WINDOW_MS < next)
            next = read_times[0] + WINDOW_MS;
        if (cas_count + count > cas_limit(current) && cas_count > 0 &&
            cas_times[0] + WINDOW_MS < next)
            next = cas_times[0] + WINDOW_MS;
        int wait = (int)(next - current);
        if (wait < 1) wait = 1;
        struct pollfd peer = { .fd = client, .events = POLLRDHUP };
        if (poll(&peer, 1, wait) < 0 && errno != EINTR) return IO;
        if (peer.revents & (POLLHUP | POLLRDHUP | POLLERR | POLLNVAL)) return BUSY;
    }
}

static enum result reserve_write(int64_t deadline, int client, bool stop_priority)
{
    for (;;) {
        enum result result = check_deadline(deadline);
        if (result != OK) return result;
        size_t capacity = write_limit(now_ms());
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
        /* Bound actual writes even when a client sends frames faster than its
         * requested 4 Hz. Use completed reservations, not packet arrival.
         * A valid Stop token bypasses pacing but not the rolling ceilings. */
        int64_t pace_current = now_ms();
        if (pace_current < 0) return IO;
        if (burst_active(pace_current) && !stop_priority && write_count) {
            int64_t gap = write_times[write_count - 1] +
                          LONG_FRAME_INTERVAL_MS - pace_current;
            if (gap > 0) {
                int64_t remaining = deadline - pace_current;
                int wait = (int)(gap < remaining ? gap : remaining);
                if (wait < 1) wait = 1;
                struct pollfd peer = { .fd = client, .events = POLLRDHUP };
                if (poll(&peer, 1, wait) < 0 && errno != EINTR) return IO;
                if (peer.revents & (POLLHUP | POLLRDHUP | POLLERR | POLLNVAL))
                    return BUSY;
                continue;
            }
        }
#endif
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
        if (!stop_priority
#if defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
            && burst_active(now_ms())
#endif
        ) {
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
            capacity = LONG_WRITE_ORDINARY;
#else
            capacity = CANDIDATE_WRITE_ORDINARY;
#endif
        }
#else
        (void)stop_priority;
#endif
        result = reserve_slot(write_times, &write_count, capacity);
        if (result == OK) {
            booked_write = true;
            booked_write_offset = write_count - 1;
        }
        if (result != BUSY) return result;
        int64_t current = now_ms();
        if (current < 0) return IO;
        int64_t until_slot = write_times[0] + WINDOW_MS - current;
        int64_t remaining = deadline - current;
        if (remaining <= 0) return BUSY;
        int wait = (int)(until_slot < remaining ? until_slot : remaining);
        if (wait < 1) wait = 1;
        struct pollfd peer = { .fd = client, .events = POLLRDHUP };
        if (poll(&peer, 1, wait) < 0 && errno != EINTR) return IO;
        if (peer.revents & (POLLHUP | POLLRDHUP | POLLERR | POLLNVAL)) return BUSY;
    }
}

static enum result write_attribute(int root, const struct stat *st,
                                   enum attribute which, const char *value,
                                   size_t size, int64_t deadline, int client,
                                   const char *cas_token, const char *cas_state,
                                   const char *cas_expected)
{
    enum result result = check_deadline(deadline);
    if (result != OK) return result;
    int fd = -1;
    result = open_attribute(root, st, which, O_WRONLY, &fd);
    if (result == OK) result = check_deadline(deadline);
    bool stop_priority = false;
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
    if (cas_token && stop_token_ready) {
        char stop_token[34];
        stop_token_string(stop_token);
        stop_priority = memcmp(cas_token, stop_token, 33) == 0;
    }
#endif
    if (result == OK) result = reserve_write(deadline, client, stop_priority);
    if (result == OK) result = check_deadline(deadline);
    if (result == OK) result = peer_present(client);
    char state[READ_MAX + 1];
    if (result == OK && cas_token) {
        char current_token[34], colors[READ_MAX + 1];
        token_string(current_token);
        if (memcmp(cas_token, current_token, 33) && !stop_priority) result = CONFLICT;
        if (result == OK) result = reserve_mutation_reads(5, deadline, client,
                                                         stop_priority);
        if (result == OK) result = read_state(root, st, state, deadline);
        if (result == OK) result = read_wire(root, st, COLORS, colors, deadline);
        if (result == OK && (strcmp(state, cas_state) ||
                            memcmp(colors, cas_expected, WIRE_LEN))) result = CONFLICT;
    } else if (result == OK) {
        result = reserve_mutation_reads(4, deadline, client, false);
        if (result == OK) result = read_state(root, st, state, deadline);
    }
    if (result == OK && strcmp(state, "on")) result = CONFLICT;

    if (result == OK) result = check_deadline(deadline);
    if (result == OK) result = peer_present(client);
    if (result == OK) {
        if (sequence == UINT64_MAX) { result = IO; goto done; }
        ++sequence; /* Any attempted write, including an uncertain one, invalidates snapshots. */
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
        stop_token_ready = false;
#endif
        ssize_t sent = write(fd, value, size); /* NEVER retry ambiguous writes. */
        if (sent < 0) result = IO; /* even EAGAIN may have changed firmware */
        else if ((size_t)sent != size) result = IO;
        if (result == OK && check_deadline(deadline) != OK) result = IO;
    }
done:
    if (fd >= 0) close(fd);
    return result;
}

static enum result transact(const char *request, size_t length,
                            char response[2 * READ_MAX + 16], int64_t deadline,
                            int client)
{
    bool status = length == 9 && memcmp(request, "1 STATUS\n", 9) == 0;
    bool restore = length == 10 && memcmp(request, "1 RESTORE\n", 10) == 0;
    bool set = length == 34 && memcmp(request, "1 SET ", 6) == 0 &&
               wire_valid(request + 6, WIRE_LEN) && request[33] == '\n';
    bool snapshot = length == 11 && memcmp(request, "2 SNAPSHOT\n", 11) == 0;
    bool stop_snapshot = length == 11 && memcmp(request, "2 STOPSHOT\n", 11) == 0;
#ifdef BROKER_RATE_EXPERIMENT
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
    bool burst = length == 8 && memcmp(request, "2 LONG4\n", 8) == 0;
#else
    bool burst = length == 10 &&
                 (memcmp(request, "2 BURST 4\n", 10) == 0 ||
                  memcmp(request, "2 BURST 5\n", 10) == 0);
#endif
#else
    bool burst = false;
#endif
    bool cas = false;
    size_t state_length = 0, expected_offset = 0, desired_offset = 0;
    if ((length == 99 || length == 100) && memcmp(request, "2 CAS ", 6) == 0 &&
        token_valid(request + 6) && request[39] == ' ') {
        state_length = length == 99 ? 2 : 3;
        expected_offset = 40 + state_length + 1;
        desired_offset = expected_offset + WIRE_LEN + 1;
        cas = ((state_length == 2 && memcmp(request + 40, "on", 2) == 0) ||
               (state_length == 3 && memcmp(request + 40, "off", 3) == 0)) &&
              request[40 + state_length] == ' ' &&
              wire_valid(request + expected_offset, WIRE_LEN) &&
              request[expected_offset + WIRE_LEN] == ' ' &&
              wire_valid(request + desired_offset, WIRE_LEN) &&
              request[desired_offset + WIRE_LEN] == '\n';
    }
    if (!status && !restore && !set && !snapshot && !stop_snapshot && !cas && !burst)
        return INVALID;

    if (burst) {
#ifdef BROKER_RATE_EXPERIMENT
        int64_t current = now_ms();
        if (current < 0) return IO;
        if (burst_used || current >= deadline) return BUSY;
        enum result claim = consume_burst_stamp();
        if (claim != OK) return claim;
        burst_used = true;
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
        burst_until = current + 15000;
#else
        burst_until = current + 3000;
#endif
#ifdef BROKER_LONG_4HZ_DIAGNOSTIC
        strcpy(response, "OK LONG4\n");
#else
        strcpy(response, "OK BURST\n");
#endif
        return OK;
#endif
    }

    int root = -1;
    struct stat st;
    enum result result = open_root(&root, &st);
    if (result == OK) result = check_abi(root, &st, deadline);
    char expected[READ_MAX + 1], actual[READ_MAX + 1];
    if (result == OK && stop_snapshot) result = reserve_stop_reads(deadline, client);
    else if (result == OK && (status || snapshot))
        result = reserve_reads(2, SNAPSHOT_READ, false);
    if (result == OK && (status || snapshot || stop_snapshot)) {
        result = read_wire(root, &st, COLORS, actual, deadline);
        if (result == OK) result = read_wire(root, &st, ORIGINAL, expected, deadline);
        if (result == OK) {
            char state[READ_MAX + 1];
            result = read_state(root, &st, state, deadline);
            if (result == OK && (snapshot || stop_snapshot)) {
                char token[34];
                token_string(token);
#if defined(BROKER_SUSTAINED_CANDIDATE) || defined(BROKER_BOUNDED_NORMAL) || defined(BROKER_LONG_4HZ_DIAGNOSTIC)
                if (stop_snapshot) {
                    stop_token_string(token);
                    stop_token_ready = true;
                }
#endif
                snprintf(response, 2 * READ_MAX + 16, "OK %s %.3s %.27s %.27s\n",
                         token, state, actual, expected);
            } else if (result == OK)
                snprintf(response, 2 * READ_MAX + 16, "OK %.3s %.27s %.27s\n",
                         state, actual, expected);
        }
    } else if (result == OK) {
        if (restore) result = read_wire(root, &st, ORIGINAL, expected, deadline);
        else if (cas) {
            memcpy(expected, request + desired_offset, WIRE_LEN);
            expected[WIRE_LEN] = '\0';
        }
        else {
            memcpy(expected, request + 6, WIRE_LEN);
            expected[WIRE_LEN] = '\0';
        }
        if (result == OK)
            result = write_attribute(root, &st, restore ? RESTORE : COLORS,
                                     restore ? "1\n" : (cas ? request + desired_offset : request + 6),
                                     restore ? 2 : WIRE_LEN + 1, deadline,
                                     client, cas ? request + 6 : NULL,
                                     cas ? (state_length == 2 ? "on" : "off") : NULL,
                                     cas ? request + expected_offset : NULL);
        if (result == OK) {
            result = read_wire(root, &st, COLORS, actual, deadline);
            if (result != OK || strcmp(actual, expected))
                result = IO; /* after write, all readback failures are uncertain */
            if (result == OK && cas) {
                char token[34];
                token_string(token);
                snprintf(response, 2 * READ_MAX + 16, "OK %s\n", token);
            } else if (result == OK) strcpy(response, "OK\n");
        }
    }
    if (root >= 0) close(root);
    return result;
}

static const char *error_response(enum result result)
{
    static const char *const responses[] = {
        "OK\n", "ERR INVALID\n", "ERR BUSY\n", "ERR MODULE\n",
        "ERR DENIED\n", "ERR IO\n", "ERR ABI\n", "ERR CONFLICT\n"
    };
    return responses[result];
}

static void serve_one(int client, int64_t deadline)
{
    struct ucred cred;
    socklen_t cred_len = sizeof(cred);
    if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &cred, &cred_len) < 0 ||
        cred_len != sizeof(cred) || cred.pid <= 0 || cred.uid == (uid_t)-1 ||
        cred.gid == (gid_t)-1) {
        (void)send(client, error_response(DENIED), strlen(error_response(DENIED)), MSG_NOSIGNAL);
        return;
    }
    enum result result = check_deadline(deadline);
    if (result != OK) goto reply;
    char packet[PACKET_MAX + 1];
    ssize_t size = recv(client, packet, sizeof(packet), MSG_DONTWAIT | MSG_TRUNC);
    if (size <= 0) { result = size < 0 ? error_from_errno(errno) : INVALID; goto reply; }
    if (size > PACKET_MAX) { result = INVALID; goto reply; }
    char excess;
    if (recv(client, &excess, 1, MSG_DONTWAIT) > 0) {
        result = INVALID;
        goto reply;
    }
    result = check_deadline(deadline);
    if (result != OK) goto reply;
    char response[2 * READ_MAX + 16];
    booked_reads = 0;
    booked_write = false;
    result = transact(packet, (size_t)size, response, deadline, client);
    complete_bookings();
    if (result == OK) {
        (void)send(client, response, strlen(response), MSG_DONTWAIT | MSG_NOSIGNAL);
        return;
    }
reply:
    (void)send(client, error_response(result), strlen(error_response(result)),
               MSG_DONTWAIT | MSG_NOSIGNAL);
}

static bool valid_listener(void)
{
    const char *fds = getenv("LISTEN_FDS");
    const char *pid = getenv("LISTEN_PID");
    char expected_pid[32];
    snprintf(expected_pid, sizeof(expected_pid), "%ld", (long)getpid());
    if (!fds || strcmp(fds, "1") || !pid || strcmp(pid, expected_pid)) return false;
    (void)unsetenv("LISTEN_FDS");
    (void)unsetenv("LISTEN_PID");
#ifndef BROKER_TESTING
    if (geteuid() != 0) return false;
#endif
    int type = 0, listening = 0;
    socklen_t size = sizeof(type);
    if (getsockopt(3, SOL_SOCKET, SO_TYPE, &type, &size) < 0 ||
        size != sizeof(type) || type != SOCK_SEQPACKET) return false;
    size = sizeof(listening);
    if (getsockopt(3, SOL_SOCKET, SO_ACCEPTCONN, &listening, &size) < 0 ||
        size != sizeof(listening) || listening != 1) return false;
    struct sockaddr_un address = {0};
    size = sizeof(address);
    if (getsockname(3, (struct sockaddr *)&address, &size) < 0 ||
        address.sun_family != AF_UNIX ||
        size <= offsetof(struct sockaddr_un, sun_path) ||
        size > sizeof(address) ||
        strnlen(address.sun_path, sizeof(address.sun_path)) != strlen(SOCKET_PATH) ||
        strcmp(address.sun_path, SOCKET_PATH)) return false;
    struct stat st;
    if (fstat(3, &st) < 0 || !S_ISSOCK(st.st_mode)) return false;
    /* Linux socket fd modes are normally 0777; the pathname inode owns ACLs. */
    if (lstat(SOCKET_PATH, &st) < 0 || !S_ISSOCK(st.st_mode) ||
        st.st_uid != geteuid() || (st.st_mode & 0007)) return false;
    int flags = fcntl(3, F_GETFL);
    return flags >= 0 && fcntl(3, F_SETFL, flags | O_NONBLOCK) == 0 &&
           fcntl(3, F_SETFD, FD_CLOEXEC) == 0;
}

int main(void)
{
    if (!valid_listener()) {
        fputs("broker: invalid systemd socket activation\n", stderr);
        return EXIT_FAILURE;
    }
    ssize_t random_size;
    do { random_size = getrandom(&nonce, sizeof(nonce), 0); }
    while (random_size < 0 && errno == EINTR);
    if (random_size != (ssize_t)sizeof(nonce)) {
        fputs("broker: cannot obtain process nonce\n", stderr);
        return EXIT_FAILURE;
    }
    struct { int fd; int64_t deadline; } pending[PENDING_MAX];
    size_t count = 0;
    for (;;) {
        struct pollfd fds[PENDING_MAX + 1] = {{ .fd = 3, .events = POLLIN }};
        int timeout = -1;
        for (size_t i = 0; i < count; ++i) {
            fds[i + 1] = (struct pollfd){ .fd = pending[i].fd,
                                          .events = POLLIN | POLLHUP | POLLERR };
            int64_t left = pending[i].deadline - now_ms();
            if (left < 0) left = 0;
            if (timeout < 0 || left < timeout) timeout = (int)left;
        }
        /* Don't accept beyond the bounded set of clients whose age we track. */
        if (count == PENDING_MAX) fds[0].events = 0;
        int ready = poll(fds, count + 1, timeout);
        if (ready < 0) {
            if (errno == EINTR) continue;
            perror("broker: poll");
            return EXIT_FAILURE;
        }
        /* Serve ready peers in acceptance order, then compact the queue. */
        for (size_t i = 0; i < count; ++i) {
            int64_t current = now_ms();
            if (current >= pending[i].deadline) {
                const char *message = error_response(BUSY);
                (void)send(pending[i].fd, message, strlen(message),
                           MSG_DONTWAIT | MSG_NOSIGNAL);
            } else if (fds[i + 1].revents & (POLLHUP | POLLERR | POLLNVAL)) {
                const char *message = error_response(INVALID);
                (void)send(pending[i].fd, message, strlen(message),
                           MSG_DONTWAIT | MSG_NOSIGNAL);
            } else if (fds[i + 1].revents & POLLIN) {
                serve_one(pending[i].fd, pending[i].deadline);
            } else {
                continue;
            }
            close(pending[i].fd);
            pending[i].fd = -1;
        }
        size_t kept = 0;
        for (size_t i = 0; i < count; ++i)
            if (pending[i].fd >= 0) pending[kept++] = pending[i];
        count = kept;
        if (fds[0].revents & POLLIN) {
            while (count < PENDING_MAX) {
                int client = accept4(3, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
                if (client < 0) {
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == ECONNABORTED) break;
                    perror("broker: accept4");
                    return EXIT_FAILURE;
                }
                int64_t current = now_ms();
                if (current < 0) {
                    close(client);
                    continue;
                }
                pending[count].fd = client;
                pending[count++].deadline = current + REQUEST_MS;
            }
        }
    }
}
