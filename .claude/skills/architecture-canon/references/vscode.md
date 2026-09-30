# VS Code — declarative contribution points

> **Principle.** Extensions describe what they add (commands, keybindings, views, settings) declaratively in a manifest; the host wires and lazily activates them.

**Forbids.** Extensions that must run their code before the host can answer "what does this extension contribute?" The declarative contribution manifest is the source of truth for the command palette, the settings UI, and help.

## Question it answers

*What does the extension contribute?*

## What "right" looks like

An extension's manifest is a strict, schema-validated declaration of everything it contributes — commands, keymaps, view kinds, themes, settings, event subscriptions. The host parses the manifest with strict schema enforcement (unknown fields are rejected) and can answer "what's in the palette?" / "what settings exist?" / "which events does this extension subscribe to?" without loading the extension's code.

Activation is lazy: the manifest is read eagerly so the host knows what's available; the extension's runtime code only runs when one of its contributions is actually invoked. Manifest declarations and runtime registration names must agree, pinned by property test.

## Red flags

- An imperative registration call at extension startup (an illustrative `host.registerCommand(...)`). → Add a `commands` entry to the manifest; the call becomes redundant.
- The palette / settings UI requires an extension to be loaded to know its commands. → The manifest is the source of truth; introspect from there.
- A contribution kind that doesn't have a manifest schema. → Add it; strict schema validation will catch typos for free.
- An extension that "lazily registers" by calling the host on first use. → The manifest declares it eagerly; runtime activates lazily. Don't conflate the two.

## Note on scope

This is a design-review canon, not a code map. The red flags above are illustrative — their bite comes from naming *your* project's actual manifest format, its contribution kinds, and the host surfaces (palette, settings, help) that read it eagerly. Translate them into those nouns when you apply them; the "declare eagerly, activate lazily, one manifest as source of truth" principle is what's durable.

## Source

*VS Code Contribution Points reference.*
