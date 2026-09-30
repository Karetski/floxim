# Behavioral scenarios

Behaviors answer **"does the observable behavior do the right thing?"** Most
tests are behaviors. Prefer to drive that behavior through the public surface —
but where your ecosystem blesses white-box unit tests (Go same-package, Rust
`#[cfg(test)]`, JS co-located), a behavior may exercise an internal directly;
see the *Placement & visibility* section of the skill.

One `behaviors_<area>` file per public subsystem, not one per source file.

## Test body shape

Illustrative — one scenario, three sections, in your language's test syntax:

```
import shared fixtures

test given_empty_collection_when_adding_one_item_then_size_is_one:
    // Given
    collection = Collection.empty()

    // When
    collection.add(item)

    // Then
    assert collection.size() == 1
```

- One scenario per test. Two `When`s in one body means two tests.
- Omit `// Given` when initial state is the type's default. Don't invent a
  synthetic `given_default_…` prefix to satisfy the long form.
- Table-driven cases are one test per row, not a loop inside one test. Failure
  messages name the row.

## Red flags

- **Test name is a verb-noun (`collection_add_works`).** Not a scenario. Rename
  to the `given_…_when_…_then_…` shape so reading the name tells you what
  behavior is being claimed.
- **Multiple `// When` headers in one body.** Two scenarios fused. Split.
- **`assert result.is_ok()` with no inspection of the value.** Either the test
  should assert on the value, or the operation doesn't deserve a fallible
  return type at that call site. Don't paper over with weak asserts.
- **Example-in-doc tests being rewritten as out-of-tree tests.** Doc examples
  stay; they document and verify simultaneously. Don't migrate them.
