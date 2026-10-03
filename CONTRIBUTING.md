# Contributing to engraph

Thanks for your interest in contributing to engraph.

## Getting started

```bash
git clone https://github.com/saagpatel/engraph
cd engraph
# See development prerequisites and checks below.
```

## Before you start

**Open an issue first.** For anything beyond a typo fix, please open an issue to discuss what you'd like to change. This avoids wasted effort if the change doesn't fit the project direction.

## Development and verification

Run commands from the checkout root containing `Cargo.toml`. Use current stable Rust/Cargo with the `rustfmt` and `clippy` components. Building llama.cpp requires CMake, a C/C++ compiler toolchain, and libclang for generated bindings. On macOS, install Xcode Command Line Tools and CMake; on Ubuntu, the corresponding packages are `build-essential`, `cmake`, `clang`, and `libclang-dev`. CI runs on macOS and Ubuntu; other platforms need separate validation.

The commands below use the committed `Cargo.lock`. The first Cargo build may fetch crates, but tests use mock models and do not download GGUF models or index a personal vault. Test stores/files are in-memory or temporary. The full library command below sets `ENGRAPH_DATA_DIR` to a new disposable directory so the `Config::load` test also avoids personal configuration.

```bash
# Build and inspect the CLI without loading config, indexing, or starting a server.
cargo build --locked
cargo run --locked -- --help

# Focused examples: module tests and the current model-free search regression.
cargo test --lib --locked store::tests
cargo test --test golden_search --locked

# Full local checks (format, lint, library tests, and the search regression).
cargo fmt --check
cargo clippy --locked -- -D warnings
ENGRAPH_DATA_DIR="$(mktemp -d)" cargo test --lib --locked
cargo test --test golden_search --locked
```

Use the relevant module/test name as a filter for other focused changes; use the same `ENGRAPH_DATA_DIR` override for tests that load config or other application data. `mktemp -d` creates a fixture-only directory on macOS/Linux without changing an existing exported data-directory setting; it can be removed after the command completes. The old `integration` test target is no longer present. CI checks formatting, Clippy, and library tests; run `golden_search` locally for search changes.

Do not use `init`, `index`, `configure`, `write`, `migrate`, or `serve` as a routine smoke check: they can read or change the configured vault and `~/.engraph/`, download models, or start watchers/servers. Real GGUF inference and live MCP/HTTP behavior require a separately isolated synthetic vault and disposable user data; the mock suite does not prove those capabilities. For changed HTTP/OpenAPI behavior, verify the affected responses in that isolated setup, adding browser checks when the changed behavior is used in a browser. Pure documentation changes do not require starting services or a browser.

## Development workflow

1. Fork the repo and create a branch from `main`
2. Write tests for any new functionality
3. Run the relevant focused checks and the full local checks above
4. Open a pull request with a clear description of what and why

## Architecture

The codebase is 20 Rust modules behind a lib crate. See `CLAUDE.md` for detailed architecture documentation — it's designed for AI-assisted development but serves as a thorough codebase guide for human contributors too.

Key modules:
- `store.rs` — SQLite persistence (all tables, queries, migrations)
- `indexer.rs` — vault walking, chunking, embedding, index updates
- `serve.rs` — MCP server with 13 tools
- `watcher.rs` — file change detection and real-time re-indexing
- `search.rs` — 3-lane hybrid search orchestration

## What makes a good contribution

- Bug fixes with tests
- Performance improvements with benchmarks
- New MCP tools that expose existing functionality
- Documentation improvements
- Platform support (Windows, other architectures)

## Code style

- Run `cargo fmt` before committing
- No clippy warnings (`cargo clippy -- -D warnings`)
- Prefer small, focused PRs over large changes
- Tests are expected for new functionality
