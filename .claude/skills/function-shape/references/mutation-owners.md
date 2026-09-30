# Mutation owners

When mutation is necessary, put it in a tiny owner with verbs that match the domain. Don't smear mutation across the call sites.

## The shape

Illustrative pseudocode — a queue of pending work items, whatever your language:

```
type PendingTaskQueue:
    tasks: list

    new()          -> empty queue
    enqueue(task)  -> append task to tasks
    has_items()    -> tasks is non-empty
    drain()        -> return all tasks AND leave tasks empty (ownership moves out)
    clear()        -> drop all tasks
```

Verbs are domain-shaped (`enqueue`, `drain`, `clear`), not container-shaped (`push`, `pop`, `reset`). The empty case is boring. Ownership crossing the boundary *moves* on `drain` — the drained items leave the owner entirely — so the caller can't accidentally share mutable state with the owner. Take-and-empty; don't hand out a live reference to the internal collection.

## Red flags

- A struct exposes mutable public fields and call sites mutate them directly. → The owner isn't owning. Wrap in named verbs or make the field private and replaced wholesale.
- A "manager" type whose every method is a thin wrapper over a list / map / handle-map. → Either the domain verbs are missing (add them) or the type isn't earning its existence (delete it; use the collection directly).
- A shared-mutable field (guarded by a lock) whose only callers lock it and mutate inline. → The shared-mutation case has no owner. Either define one, or move to message-passing so state changes through one channel instead of ambient locking.
