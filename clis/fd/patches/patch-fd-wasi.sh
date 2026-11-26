#!/usr/bin/env bash
# Patch fd for the wasm32-wasip2 Rust lane. Run from the fd source dir by the
# eryx-build Rust lane (manifest `vendor_patch`). Idempotent.
#
# wasip2 has no threads, and fd uses them for everything:
#   - its walk is ignore's WalkParallel, which spawns its workers in a
#     thread::scope even for one thread, while the results are received and
#     printed on another thread;
#   - --exec runs its commands on a pool of threads;
#   - ctrlc (for ^C while colouring) runs its handler on a thread, and has no
#     WASI backend at all, so it doesn't compile.
# On WASI (only: other targets build as upstream) this:
#   - walks with ignore's sequential Walk instead, through run_walker, which
#     drives the same per-entry visitor fd gives WalkParallel. Walk has no
#     public way to skip a directory, so a visitor's WalkState::Skip (--prune,
#     --ignore-contain) drops the entries under that directory instead (they
#     are still read, but never matched or printed);
#   - fills an unbounded channel with the whole walk first, then receives and
#     prints it on the same thread (so output is still sorted for up to 1000
#     results, as upstream's buffering does for fast searches);
#   - runs --exec's jobs on the main thread, and pins the thread count to 1;
#   - drops ctrlc (a target-specific dependency, so Cargo.lock still matches).
# jemalloc doesn't build for wasm either; the manifest turns off its feature.
set -euo pipefail

for f in Cargo.toml src/main.rs src/walk.rs; do
    [ -f "$f" ] || { echo "patch-fd-wasi: $f not found (wrong dir?)" >&2; exit 1; }
done
if grep -q 'eryx: wasi' src/walk.rs; then
    echo "patch-fd-wasi: already applied"
    exit 0
fi

# replace FILE OLD NEW: replace the one literal occurrence of OLD with NEW,
# failing if OLD isn't there exactly once (upstream changed).
replace() {
    OLD="$2" NEW="$3" perl -0pi -e '
        my ($o, $n) = ($ENV{OLD}, $ENV{NEW});
        my $count = () = /\Q$o\E/g;
        die "patch-fd-wasi: ERROR: expected one match in $ARGV, found $count:\n$o\n" unless $count == 1;
        s/\Q$o\E/$n/;
    ' "$1"
}

# ctrlc: only off WASI.
replace Cargo.toml $'ctrlc = "3.5"\n' ''
cat >> Cargo.toml <<'EOF'

# eryx: wasi has no signals (or threads for ctrlc's handler).
[target.'cfg(not(target_os = "wasi"))'.dependencies]
ctrlc = "3.5"
EOF

# fd's filesystem and options code only covers unix and windows. WASI has no
# devices, sockets or pipes in its filesystems to find (-t b/c/s/p never
# match), and nothing to stop --one-file-system crossing (so it's not an
# option, as upstream does on other platforms).
perl -0pi -e '
    my $count = s/#\[cfg\(windows\)\]\n(pub fn is_(?:block_device|char_device|socket|pipe)\(_: fs::FileType\))/#[cfg(not(any(unix, target_os = "redox")))]\n$1/g;
    die "patch-fd-wasi: ERROR: expected 4 windows-only file type checks, found $count\n" unless $count == 4;
' src/filesystem.rs
replace src/main.rs \
    $'        one_file_system: opts.one_file_system,\n' \
    $'        #[cfg(any(unix, windows))]\n        one_file_system: opts.one_file_system,\n        #[cfg(not(any(unix, windows)))]\n        one_file_system: false,\n'

# One thread on WASI, whatever -j says.
replace src/main.rs \
    'threads: opts.threads().get(),' \
    'threads: if cfg!(target_os = "wasi") { 1 } else { opts.threads().get() },'

replace src/walk.rs \
    'use ignore::{WalkBuilder, WalkParallel, WalkState};' \
    'use ignore::{WalkBuilder, WalkState};'

# build_walker returns the builder; run_walker builds the walk it can run.
replace src/walk.rs \
    'fn build_walker(&self, paths: &[PathBuf]) -> Result<WalkParallel> {' \
    'fn build_walker(&self, paths: &[PathBuf]) -> Result<WalkBuilder> {'
replace src/walk.rs \
    $'let walker = builder.threads(config.threads).build_parallel();\n        Ok(walker)' \
    $'builder.threads(config.threads);\n        Ok(builder)'
replace src/walk.rs \
    $'fn spawn_senders(&self, walker: WalkParallel, tx: Sender<Batch>) {\n        walker.run(|| {' \
    $'fn spawn_senders(&self, walker: WalkBuilder, tx: Sender<Batch>) {\n        run_walker(walker, || {'

# --exec's jobs: on this thread.
replace src/walk.rs \
    $'                exec::batch(rx.into_iter().flatten(), cmd, config)\n            } else {\n' \
    $'                exec::batch(rx.into_iter().flatten(), cmd, config)\n            } else if cfg!(target_os = "wasi") {\n                exec::job(rx.into_iter().flatten(), cmd, config)\n            } else {\n'

# No ^C handler on WASI.
replace src/walk.rs \
    $'        if config.ls_colors.is_some() && config.is_printing() {\n            let quit_flag' \
    $'        #[cfg(not(target_os = "wasi"))]\n        if config.ls_colors.is_some() && config.is_printing() {\n            let quit_flag'

# Walk, then receive, on this thread.
replace src/walk.rs \
    $'        let (tx, rx) = bounded(2 * config.threads);\n\n        let exit_code = thread::scope(|scope| {' \
    $'        // eryx: wasi has no threads: walk everything, then print it.\n        #[cfg(target_os = "wasi")]\n        let exit_code = {\n            let (tx, rx) = crossbeam_channel::unbounded();\n            self.spawn_senders(walker, tx);\n            self.receive(rx)\n        };\n\n        #[cfg(not(target_os = "wasi"))]\n        let (tx, rx) = bounded(2 * config.threads);\n\n        #[cfg(not(target_os = "wasi"))]\n        let exit_code = thread::scope(|scope| {'

cat >> src/walk.rs <<'EOF'

/// ignore's own (private) visitor type, which WalkParallel::run takes.
type FnVisitor<'s> =
    Box<dyn FnMut(Result<ignore::DirEntry, ignore::Error>) -> WalkState + Send + 's>;

#[cfg(not(target_os = "wasi"))]
fn run_walker<'s, F: FnMut() -> FnVisitor<'s>>(builder: WalkBuilder, mkf: F) {
    builder.build_parallel().run(mkf)
}

/// eryx: wasi has no threads, so run the visitor WalkParallel would have run
/// over the sequential walk instead. Walk can't be told to skip a directory,
/// so a Skip drops the entries under it.
#[cfg(target_os = "wasi")]
fn run_walker<'s, F: FnMut() -> FnVisitor<'s>>(builder: WalkBuilder, mut mkf: F) {
    let mut visitor = mkf();
    let mut skipped: Option<PathBuf> = None;
    for entry in builder.build() {
        let dir = match &entry {
            Ok(e) => {
                if skipped.as_deref().is_some_and(|s| e.path() != s && e.path().starts_with(s)) {
                    continue;
                }
                e.file_type()
                    .is_some_and(|t| t.is_dir())
                    .then(|| e.path().to_path_buf())
            }
            Err(_) => None,
        };
        match visitor(entry) {
            WalkState::Continue => {}
            WalkState::Skip => {
                if dir.is_some() {
                    skipped = dir;
                }
            }
            WalkState::Quit => break,
        }
    }
}
EOF

echo "patch-fd-wasi: applied (sequential walk, no threads or ctrlc on wasi)"
