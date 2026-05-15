---
bundle:
  name: nanoclaw-amp
  version: 0.1.0
  description: build-up bundle + provider-anthropic for nanoclaw-on-amplifier POC

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=experiments/build-up/build-up-foundation.md

providers:
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      default_model: claude-sonnet-4-5
---
