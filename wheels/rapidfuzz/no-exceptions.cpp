// eryx's libc++abi is built without exception support (wasmtime would also
// need the exception-handling proposal), so it has no __cxa_throw. Without
// -fwasm-exceptions, clang still compiles `throw` and `try`, but lowers every
// call to a plain call: catch blocks can never run, and a throw goes to
// __cxa_throw. These stand in for libc++abi's, so a throw aborts the run
// instead of the extension failing to load. Hidden, so each module keeps its
// own and none are exported.
//
// rapidfuzz mostly reports bad input from Cython, before any C++ runs, but
// not always: utils.default_process(1) raises TypeError on Linux and aborts
// here.
#include <cstdio>
#include <cstdlib>
#include <typeinfo>

#define HIDDEN __attribute__((visibility("hidden")))

extern "C" {

HIDDEN void* __cxa_allocate_exception(size_t size) noexcept
{
    return std::malloc(size);
}

HIDDEN void __cxa_free_exception(void* thrown) noexcept
{
    std::free(thrown);
}

[[noreturn]] HIDDEN void __cxa_throw(void*, std::type_info* type, void (*)(void*))
{
    std::fprintf(stderr, "rapidfuzz: C++ exception %s thrown, but this WebAssembly build can't catch it\n",
                 type->name());
    std::abort();
}

}
