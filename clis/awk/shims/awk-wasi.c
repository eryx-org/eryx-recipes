/* awk's pipes (`cmd | getline`, `print | cmd`) and system() need a shell,
 * which a WASI command can't start. wasi-libc has no popen() or system(), so
 * these stand in and say why on stderr:
 * - `cmd | getline` stops awk (FATAL, exit 2). Failing softly would make
 *   getline return -1, which is true, so `while (cmd | getline x)` would
 *   never end.
 * - `print | cmd` fails the open, and awk stops with "can't open file".
 * - system() returns -1 and awk carries on. */
#include <errno.h>
#include <stdio.h>
#include <string.h>

/* awk's own (lib.c): prints "awk: ...", the source line, and exits 2. */
extern _Noreturn void FATAL(const char *fmt, ...);

static void unsupported(const char *what, const char *cmd) {
    fprintf(stderr, "awk: can't run \"%s\": %s isn't supported in this sandbox\n", cmd, what);
}

FILE *popen(const char *cmd, const char *mode) {
    if (strchr(mode, 'r') != NULL)
        FATAL("can't run \"%s\": a pipe from a command isn't supported in this sandbox", cmd);
    unsupported("a pipe to a command", cmd);
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
