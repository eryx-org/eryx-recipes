/* awk's pipes (`cmd | getline`, `print | cmd`) and system() need a shell,
 * which a WASI command can't start. wasi-libc has no popen() or system(), so
 * these stand in: each says why on stderr and fails, so awk carries on as it
 * does when a command can't be run (getline returns -1, system() returns -1,
 * and `print | cmd` stops with "can't open file"). */
#include <errno.h>
#include <stdio.h>

static void unsupported(const char *what, const char *cmd) {
    fprintf(stderr, "awk: can't run \"%s\": %s isn't supported in this sandbox\n", cmd, what);
}

FILE *popen(const char *cmd, const char *mode) {
    (void)mode;
    unsupported("a pipe to or from a command", cmd);
    errno = ENOSYS;
    return NULL;
}

int pclose(FILE *f) {
    (void)f;
    errno = ENOSYS;
    return -1;
}

int system(const char *cmd) {
    if (cmd == NULL)
        return 0; /* no shell available */
    unsupported("system()", cmd);
    errno = ENOSYS;
    return -1;
}
