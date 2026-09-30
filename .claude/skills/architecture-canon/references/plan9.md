# Plan 9 — uniform addressing via namespaces

> **Principle.** Every resource — local, remote, in-memory, hardware — is a node in a hierarchical namespace, accessed through one interface (in Plan 9, the 9P file protocol: everything is a file).

**Fidelity note.** The paper's actual novelty is that namespaces are *per-process* — each process composes its own view by mounting resources where it wants them — not that there is one global tree. The durable design lesson borrowed here is the uniform-addressing half (one grammar, one resolution interface for every resource kind); if you also need dynamic per-context composition, that's the deeper Plan 9 idea.

**Forbids.** A second addressing scheme. Distinct resource kinds — documents, views, external services, sub-processes, jobs, settings — live at paths in one namespace, not in several ad-hoc registries with different lookup APIs.

## Question it answers

*How is it addressed?*

## What "right" looks like

There is one address type — a path with a single, validated grammar. Every resource (documents, views, external services, settings, sub-processes, jobs) lives under that path namespace, and there is one resolution interface that turns a path into the resource. The grammar is enforced at parse time; roundtrip stability is pinned by property test.

When a new subsystem appears, its resources get a path prefix in the existing namespace; they don't get a new id type and a new lookup function.

## Red flags

- A new hash-map registry keyed by some id and exposed via its own `get_foo(id)` accessor. → Move into the namespace under a fresh path prefix.
- A second URI/URL scheme for "internal" resources. → One namespace, one grammar.
- An id type used outside its module as an opaque handle, with a separate lookup function. → Address it by path; let the namespace own the resolution.
- A subsystem with both an in-memory registry *and* a path representation. → The path is the source of truth; the registry is implementation detail.

## Source

Pike, Presotto, Thompson, Trickey, Winterbottom, *The Use of Name Spaces in Plan 9*, 1992.
