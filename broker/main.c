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
#define READ_LIMIT 25 /* 5Hz: two snapshots, two CAS checks, one verification */
#define STATUS_READ_LIMIT 10 /* preserve room for verified writes */
#define CAS_READ_LIMIT 10
#define WINDOW_MS 1000
#define WIRE_LEN 27

enum result { OK, INVALID, BUSY, MODULE, DENIED, IO, ABI, CONFLICT };
enum read_kind { SNAPSHOT_READ, CAS_READ, VERIFY_READ };
enum attribute { ABI_VERSION, COLORS, ORIGINAL, STATE, RESTORE };
static const char *const attributes[] = {
    "abi_version", "colors", "original", "state", "restore"
};
static const mode_t attribute_modes[] = { 0444, 0600, 0444, 0400, 0200 };
static int64_t write_times[WRITE_LIMIT];
static size_t write_count;
static int64_t read_times[READ_LIMIT];
static size_t read_count;
static int64_t status_times[STATUS_READ_LIMIT];
static size_t status_count;
static int64_t cas_times[CAS_READ_LIMIT];
static size_t cas_count;
static uint64_t nonce, sequence;

static void token_string(char out[34])
{
    snprintf(out, 34, "%016" PRIX64 ":%016" PRIX64, nonce, sequence);
}

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
    if (*count == capacity) return BUSY;
    times[(*count)++] = current; /* failed attempts consume budget */
    return OK;
}

/* Admit the whole STATUS pair atomically; reserve write verification separately. */
static enum result reserve_reads(size_t count, enum read_kind kind)
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
    if (read_count + count > READ_LIMIT ||
        (kind == SNAPSHOT_READ && status_count + count > STATUS_READ_LIMIT) ||
        (kind == CAS_READ && cas_count + count > CAS_READ_LIMIT)) return BUSY;
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

static enum result reserve_write(int64_t deadline, int client)
{
    for (;;) {
        enum result result = check_deadline(deadline);
        if (result != OK) return result;
        result = reserve_slot(write_times, &write_count, WRITE_LIMIT);
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
    if (result == OK) result = reserve_write(deadline, client);
    if (result == OK) result = check_deadline(deadline);
    if (result == OK) result = peer_present(client);
    if (result == OK && cas_token) {
        char current_token[34], state[READ_MAX + 1], colors[READ_MAX + 1];
        token_string(current_token);
        if (memcmp(cas_token, current_token, 33)) result = CONFLICT;
        if (result == OK) result = reserve_reads(2, CAS_READ);
        if (result == OK) result = read_state(root, st, state, deadline);
        if (result == OK) result = read_wire(root, st, COLORS, colors, deadline);
        if (result == OK && (strcmp(state, cas_state) ||
                            memcmp(colors, cas_expected, WIRE_LEN))) result = CONFLICT;
    }
    if (result == OK) result = reserve_reads(1, VERIFY_READ); /* before write */
    if (result == OK) result = check_deadline(deadline);
    if (result == OK) result = peer_present(client);
    if (result == OK) {
        if (sequence == UINT64_MAX) { result = IO; goto done; }
        ++sequence; /* Any attempted write, including an uncertain one, invalidates snapshots. */
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
    if (!status && !restore && !set && !snapshot && !cas) return INVALID;

    int root = -1;
    struct stat st;
    enum result result = open_root(&root, &st);
    if (result == OK) result = check_abi(root, &st, deadline);
    char expected[READ_MAX + 1], actual[READ_MAX + 1];
    if (result == OK && (status || snapshot)) result = reserve_reads(2, SNAPSHOT_READ);
    if (result == OK && (status || snapshot)) {
        result = read_wire(root, &st, COLORS, actual, deadline);
        if (result == OK) result = read_wire(root, &st, ORIGINAL, expected, deadline);
        if (result == OK) {
            char state[READ_MAX + 1];
            result = read_state(root, &st, state, deadline);
            if (result == OK && snapshot) {
                char token[34];
                token_string(token);
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
    result = transact(packet, (size_t)size, response, deadline, client);
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
