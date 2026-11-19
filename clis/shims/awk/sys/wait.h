/* awk's run.c decodes system()'s and pclose()'s status with these; wasi-libc
 * has no <sys/wait.h>. The stubs in awk-wasi.c never return a status, so
 * only the definitions matter. Copied into awk's source by patch-awk.sh. */
#pragma once
#define WIFEXITED(s)   (((s) & 0x7f) == 0)
#define WEXITSTATUS(s) (((s) >> 8) & 0xff)
#define WIFSIGNALED(s) (((s) & 0x7f) != 0 && ((s) & 0x7f) != 0x7f)
#define WTERMSIG(s)    ((s) & 0x7f)
