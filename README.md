# public-actions

Reusable GitHub Actions workflows for Swift projects and Homebrew taps.

The workflows here are designed for public consumption: any external caller can `uses:` them without depending on internal infrastructure. Internal callers can layer their own conventions on top (runner selection, secret management) without forking.

## Workflows

| Workflow | Purpose |
|---|---|
| [`.github/workflows/swift-test.yml`](.github/workflows/swift-test.yml) | Build and test a Swift package or Xcode project on macOS (SPM, `xcodebuild test`, multi-target app builds). |
| [`.github/workflows/deploy-docc.yml`](.github/workflows/deploy-docc.yml) | Build Swift DocC documentation and publish to GitHub Pages. |

Each workflow's own header comment carries detailed input/secret reference and example callers — start there.

## Runner selection: the `vars.RUNNER_*` pattern

Every workflow that runs on a hosted or self-hosted runner accepts a `runner` input. The **recommended caller pattern** is:

```yaml
# macOS workflow
with:
  runner: ${{ vars.RUNNER_MACOS || '["macos-26"]' }}

# Linux workflow
with:
  runner: ${{ vars.RUNNER_LINUX || '["ubuntu-latest"]' }}
```

### Why this pattern

- **`vars.RUNNER_MACOS` / `vars.RUNNER_LINUX`** are org- or repo-level GitHub Variables (Settings → Variables → Actions). When set, every caller workflow in that scope automatically uses the same runner, which makes it trivial to flip an entire org between GitHub-hosted and self-hosted runners with a single variable edit.
- The **`|| '["macos-26"]'` fallback** kicks in when the variable isn't defined — e.g. for fresh adopters who don't yet have a runner-selection convention. The workflow still works without any configuration.

> [!NOTE]
> The value is a **JSON array string** because the workflow's `runs-on:` line uses `fromJson(inputs.runner)`. This lets you point at compound runner labels (e.g. `'["self-hosted","macos","arm64"]'`) without changing the workflow contract. A bare label like `macos-26` (no surrounding `["..."]`) will fail the `fromJson` step.

### Setting `vars.RUNNER_MACOS` for your org

```
Settings → Secrets and variables → Actions → Variables tab → New repository variable
  Name: RUNNER_MACOS
  Value: ["self-hosted","macos","arm64"]
```

(Or at org level: `Org settings → Secrets and variables → Actions → Variables`.)

If you skip this entirely, the workflow's input default — `'["macos-26"]'` for macOS, `'["ubuntu-latest"]'` for Linux — is what runs.

## Required permissions and secrets

Each workflow's header documents its specific needs. Brief summary:

- **`swift-test.yml`** — `contents: read`. Optional `swift-pm-auth-token` secret for SPM packages with private dependencies.
- **`deploy-docc.yml`** — `contents: read`, `pages: write`, `id-token: write`. Optional `swift-pm-auth-token` for private SPM deps.

## Caller prerequisites for `deploy-docc.yml`

The SPM build path uses `swift package generate-documentation`, which is registered by the [swift-docc-plugin](https://github.com/apple/swift-docc-plugin) Swift Package Manager plugin. The workflow does **not** auto-inject this dependency — your `Package.swift` must include it explicitly:

```swift
dependencies: [
    // ... your other dependencies
    .package(url: "https://github.com/apple/swift-docc-plugin", from: "1.0.0"),
]
```

Without it, the workflow fails with: `error: Unknown subcommand or plugin name 'generate-documentation'`.

The workflow also checks out and builds the canonical Apple [`swiftlang/swift-docc-render`](https://github.com/swiftlang/swift-docc-render) on every run, giving callers a fully-styled DocC site by default. Override `docc-render-repo` and `docc-render-ref` to swap in a fork (e.g. one with Mermaid diagram support).

## License

[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) — see the [`LICENSE`](LICENSE) file for the full legal text.
