/*
 * eryx's libc++abi is built without exceptions, so it has no __cxa_throw.
 * pandas' one C++ extension (window/aggregations) only throws from libc++'s
 * inline std::bad_array_new_length, so a throw aborts, as libc++'s own do
 * without exceptions. The same stub as wheels/numpy's.
 */
#include <stdio.h>
#include <stdlib.h>

void *__cxa_allocate_exception(size_t size) { return malloc(size); }

void __cxa_throw(void *exception, void *type, void (*destructor)(void *)) {
    fputs("pandas: C++ exception thrown, but exceptions aren't supported\n", stderr);
    abort();
}
