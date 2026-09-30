# LSP — narrow protocol with optional capabilities

> **Principle.** A narrow versioned protocol with optional capability flags turns an M×N integration problem into M+N and keeps old clients working as new capabilities land.

**Forbids.** An extension API that is a language-level interface a plugin must implement in the host's source language. Extension is a versioned protocol with negotiated capabilities — and the valuable move is to apply it *internally*, between core and its own extensions, not just externally to third-party services.

## Question it answers

*How does an extension reach it?*

## What "right" looks like

The extension surface is a versioned protocol — a `(major, minor)` pair on the wire, with capability bits negotiated independently per axis (protocol, event bus, contribution manifest). An extension asks for the capabilities it wants; the host responds with the subset it supports. Capability mismatch degrades gracefully: missing capabilities cause the affected feature to be unavailable, not the extension to fail loading.

Old extensions keep working as new capabilities land because each new feature gates behind a new capability bit. Major-version bumps are the only breaking change, and they're rare.

## Red flags

- A public interface in the host's source language that extensions must implement (e.g. an illustrative `interface HostPlugin`). → That recreates M×N. Define a versioned protocol surface with capability bits.
- A new feature added without a capability flag. → Old extensions must keep loading; add the bit and gate the new shape behind it.
- A breaking schema change without a `major` bump. → Either bump major or introduce a capability that gates the new shape behind explicit opt-in.
- A "trusted-internal" path that bypasses the protocol. → If core uses a privileged shortcut, extensions are second-class; collapse the shortcut into the protocol.

## Source

*Language Server Protocol Specification*.
