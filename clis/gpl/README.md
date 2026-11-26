# GPL-licensed CLI recipes

Recipes here build commands whose source is GPL-licensed, kept apart from the
rest of `clis/` so a licence audit can tell them apart at a glance
(eryx-org/eryx#837). Being here is what puts a command in the `eryx-gpl`
namespace.

- Each subdirectory is one command, laid out as any command's directory in
  `clis/` is (see the top-level README's Commands section), plus a
  `NOTICE.md`. Everything in it (the manifest, patches, shims and scripts) is
  under that command's licence, given in its `NOTICE.md`. What it uses from
  outside it (`clis/shared/`, `wit/`) keeps its own licence.
- The components they build are separate artifacts. They are never linked
  into or embedded in the eryx crates, the `eryx` binary or any other
  MIT/Apache-licensed artifact; a sandbox loads them at run time like any
  other command.

Build one with `mise run build-cli -- <name>`, as any other.
