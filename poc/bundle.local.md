---
bundle:
  name: nanoclaw-amp-local
  version: 0.1.0
  description: build-up bundle + provider-anthropic for local POC (no network on includes)

includes:
  - bundle: file:///home/bkrabach/dev/aaa-claw/amplifier-foundation/experiments/build-up/build-up-foundation.md

providers:
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      default_model: claude-sonnet-4-5
---
