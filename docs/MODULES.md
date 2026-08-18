# First-party module contract

The deliberately small `ModuleProvider` contract contains:

```python
module_id: str
title: str
description: str
create_view(parent, context) -> Widget
```

`ModuleContext` provides a data directory, demo flag, vendor-neutral session gate, a small service map, and operation-state callback. Providers are returned explicitly by `first_party_modules()`. This keeps review and packaging deterministic and avoids premature third-party runtime plugin loading.

A new first-party module should:

1. Put vendor/product details below `operations_toolkit/modules/<provider>/`.
2. Implement the four contract members and add one explicit registry entry.
3. Keep live and demo adapters on the same narrow protocol; demo must be network-impossible.
4. Add contract, domain, persistence, safety, UI construction, and `--smoke-test` coverage.
5. Document state transitions, recovery semantics, and credential lifecycle.

Shared core must remain vendor-neutral. Do not move cnMaestro endpoints, templates, rate models, or job payloads into core.
