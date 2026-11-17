/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * File descriptors for live pipes to commands started through
 * eryx:process/pipe, which git's run-command.c (git-https.patch) hands out
 * as cmd->in and cmd->out. WASI can't make file descriptors, so these are
 * numbers from VFD_BASE up that read, write, close and fdopen (wrapped at
 * link time with -Wl,--wrap) recognise, passing every other fd through;
 * fdopen gives them a FILE through fopencookie. dup (wasi-git-stubs.c)
 * counts another reference.
 *
 * Also: files to remove once their last descriptor is closed
 * (wasi_unlink_on_close), for start_async's spool files.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "child.h"

/* musl's, hidden here because wasi-git.h included stdio.h without
 * _GNU_SOURCE. */
typedef struct {
	ssize_t (*read)(void *, char *, size_t);
	ssize_t (*write)(void *, const char *, size_t);
	int (*seek)(void *, off_t *, int);
	int (*close)(void *);
} wasi_cookie_io_t;
FILE *fopencookie(void *, const char *, wasi_cookie_io_t);

#define VFD_BASE 1000
#define MAX_SLOTS 32

struct slot {
	int used, waited;
	eryx_process_pipe_own_piped_t own;
	int refs[2]; /* open fds: [0] read (its stdout), [1] write (its stdin) */
	int stdin_closed;
};
static struct slot slots[MAX_SLOTS];

ssize_t __real_read(int, void *, size_t);
ssize_t __real_write(int, const void *, size_t);
int __real_close(int);
FILE *__real_fdopen(int, const char *);

static eryx_process_pipe_borrow_piped_t borrow(struct slot *s)
{
	return eryx_process_pipe_borrow_piped(s->own);
}

static struct slot *vfd_slot(int fd, int *writing)
{
	int i = fd - VFD_BASE;
	if (i < 0 || i >= 2 * MAX_SLOTS || !slots[i / 2].used)
		return NULL;
	*writing = i % 2;
	return &slots[i / 2];
}

int wasi_piped_register(uint32_t handle)
{
	int i;
	for (i = 0; i < MAX_SLOTS; i++) {
		if (!slots[i].used) {
			memset(&slots[i], 0, sizeof(slots[i]));
			slots[i].used = 1;
			slots[i].own.__handle = handle;
			return i;
		}
	}
	return -1;
}

int wasi_piped_fd(int slot, int writing)
{
	slots[slot].refs[writing]++;
	return VFD_BASE + 2 * slot + writing;
}

static void close_stdin(struct slot *s)
{
	if (!s->stdin_closed) {
		s->stdin_closed = 1;
		eryx_process_pipe_method_piped_close_stdin(borrow(s));
	}
}

void wasi_piped_close_stdin(int slot)
{
	close_stdin(&slots[slot]);
}

static ssize_t piped_read(struct slot *s, void *buf, size_t n)
{
	child_list_u8_t got;
	eryx_process_pipe_run_error_t err;
	if (!eryx_process_pipe_method_piped_read(borrow(s), n, &got, &err)) {
		errno = EIO;
		return -1;
	}
	if (got.len > n)
		got.len = n;
	memcpy(buf, got.ptr, got.len);
	/* An empty list's pointer is dangling, not allocated. */
	if (got.len)
		free(got.ptr);
	return got.len;
}

static ssize_t piped_write(struct slot *s, const void *buf, size_t n)
{
	child_list_u8_t data = { (uint8_t *)buf, n };
	uint64_t wrote;
	eryx_process_pipe_run_error_t err;
	if (!eryx_process_pipe_method_piped_write(borrow(s), &data, &wrote, &err)) {
		errno = EPIPE;
		return -1;
	}
	return wrote;
}

/* Read the child's stdout to its end, into fd (or nowhere if fd < 0). */
void wasi_piped_drain(int slot, int fd)
{
	char buf[16384];
	ssize_t n;
	while ((n = piped_read(&slots[slot], buf, sizeof(buf))) > 0)
		if (fd >= 0)
			write(fd, buf, n);
}

int wasi_piped_wait(int slot)
{
	struct slot *s = &slots[slot];
	int32_t code = -1;
	eryx_process_pipe_run_error_t err;
	close_stdin(s);
	if (!eryx_process_pipe_method_piped_wait(borrow(s), &code, &err))
		code = -1;
	s->waited = 1;
	if (!s->refs[0] && !s->refs[1]) {
		eryx_process_pipe_piped_drop_own(s->own);
		s->used = 0;
	}
	return code;
}

/* Files to remove when the descriptor reading them is closed. */
#define MAX_UNLINKS 8
static struct {
	int fd;
	char *path;
} unlinks[MAX_UNLINKS];

void wasi_unlink_on_close(int fd, const char *path)
{
	int i;
	for (i = 0; i < MAX_UNLINKS; i++) {
		if (!unlinks[i].path) {
			unlinks[i].fd = fd;
			unlinks[i].path = strdup(path);
			return;
		}
	}
	/* No room: it stays until the execution's /tmp goes. */
}

static void unlink_closed(int fd)
{
	int i;
	for (i = 0; i < MAX_UNLINKS; i++) {
		if (unlinks[i].path && unlinks[i].fd == fd) {
			unlink(unlinks[i].path);
			free(unlinks[i].path);
			unlinks[i].path = NULL;
		}
	}
}

ssize_t __wrap_read(int fd, void *buf, size_t n)
{
	int w;
	struct slot *s = vfd_slot(fd, &w);
	if (!s)
		return __real_read(fd, buf, n);
	if (w) {
		errno = EBADF;
		return -1;
	}
	return piped_read(s, buf, n);
}

ssize_t __wrap_write(int fd, const void *buf, size_t n)
{
	int w;
	struct slot *s = vfd_slot(fd, &w);
	if (!s)
		return __real_write(fd, buf, n);
	if (!w) {
		errno = EBADF;
		return -1;
	}
	return piped_write(s, buf, n);
}

int __wrap_close(int fd)
{
	int w;
	struct slot *s = vfd_slot(fd, &w);
	if (!s) {
		int ret = __real_close(fd);
		unlink_closed(fd);
		return ret;
	}
	if (s->refs[w] > 0 && !--s->refs[w] && w)
		close_stdin(s);
	if (s->waited && !s->refs[0] && !s->refs[1]) {
		eryx_process_pipe_piped_drop_own(s->own);
		s->used = 0;
	}
	return 0;
}

int wasi_vfd_dup(int fd)
{
	int w;
	struct slot *s = vfd_slot(fd, &w);
	if (!s) {
		errno = ENOSYS;
		return -1;
	}
	s->refs[w]++;
	return fd;
}

static ssize_t cookie_read(void *c, char *buf, size_t n)
{
	return __wrap_read((int)(intptr_t)c, buf, n);
}

static ssize_t cookie_write(void *c, const char *buf, size_t n)
{
	return __wrap_write((int)(intptr_t)c, buf, n);
}

static int cookie_close(void *c)
{
	return __wrap_close((int)(intptr_t)c);
}

FILE *__wrap_fdopen(int fd, const char *mode)
{
	int w;
	wasi_cookie_io_t io = { cookie_read, cookie_write, NULL, cookie_close };
	if (!vfd_slot(fd, &w))
		return __real_fdopen(fd, mode);
	return fopencookie((void *)(intptr_t)fd, mode, io);
}
