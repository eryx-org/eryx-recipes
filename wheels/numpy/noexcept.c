/*
 * eryx's libc++abi is built without exceptions, so it has no __cxa_throw.
 * numpy's C++ (pocketfft, unique_hash) can throw, mostly std::bad_alloc, so
 * give the extensions that do these: a throw aborts, as libc++'s own do
 * without exceptions.
 */
#include <stdio.h>
#include <stdlib.h>

void *__cxa_allocate_exception(size_t size) { return malloc(size); }

void __cxa_throw(void *exception, void *type, void (*destructor)(void *)) {
    fputs("numpy: C++ exception thrown, but exceptions aren't supported\n", stderr);
    abort();
}
