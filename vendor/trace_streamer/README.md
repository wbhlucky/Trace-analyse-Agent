# Bundled Trace Streamer

This directory contains project-local Trace Streamer 4.3.7 binaries used to
convert HTrace files into SQLite databases.

Supported bundled targets:

```text
windows-x86_64/trace_streamer.exe
linux-x86_64/trace_streamer
darwin-aarch64/trace_streamer
```

The binaries were sourced from the `assets/trace_streamer` directory of:

```text
https://gitcode.com/diting/hmtrace.git
commit 6ad07fb5c1415eed20d25c4538291e93e7439c99
```

The included `LICENSE` file contains the Apache License 2.0 text distributed
with that source project.

The runtime never scans the host filesystem for another Trace Streamer. An
explicit CLI path can override the bundled binary for development and
unsupported platforms.
