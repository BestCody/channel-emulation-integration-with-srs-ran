# Temporal NRX C-RNTI Runtime Hook

This document defines the scheduler/runtime boundary for the temporal UE-memory
receiver implemented in `BestCody/neural_rx` on branch
`experiment/temporal-ue-memory-training`.

## Ownership rule

The neural network does **not** infer UE identity from RF samples. The gNB owns
UE identity. The stable temporal-memory key is the C-RNTI already associated
with each scheduled PUSCH.

The receiver-side bridge is:

```text
neural_rx/scripts/crnti_memory_adapter.py
```

It wraps `RuntimeUEMemoryManager` and provides:

- `lookup(crntis, slot_index)`
- `process_result(lookup, next_memory, slot_index, active=None)`
- `release(crnti)`
- `handover(crnti)`
- `reestablishment(old_crnti)`
- `expire(slot_index)`
- `clear()`

## Required PUSCH call sequence

At the point where srsRAN has resolved the scheduled PUSCH PDUs for a slot and
is about to invoke the temporal neural receiver:

```text
scheduled PUSCH PDUs
        |
        +--> C-RNTI for current receiver position 0
        +--> C-RNTI for current receiver position 1
        |
        v
CRNTIMemoryAdapter.lookup(crntis, slot_index)
        |
        +--> prev_memory [num_ues, d_mem]
        +--> gap_slots   [num_ues]
        +--> valid       [num_ues]
        |
        v
temporal NRX inference
        |
        +--> decoded result
        +--> next_memory [num_ues, d_mem]
        |
        v
CRNTIMemoryAdapter.process_result(lookup, next_memory, slot_index)
```

Use the immutable `RuntimeMemoryInput` returned by `lookup` when committing the
result. This prevents a position-order change from writing memory under the
wrong C-RNTI.

## Lifecycle hooks

Use these conservative state rules:

```text
brief scheduling absence
    -> no removal; memory is kept

return before expiry
    -> lookup restores the same C-RNTI memory

absence longer than expiry_slots
    -> adapter/manager expires and zeroes the row

UE release
    -> release(crnti)

handover
    -> handover(crnti) and cold-start in the new context

RRC re-establishment / identity replacement
    -> reestablishment(old_crnti)
```

Freed rows are zeroed by `RuntimeUEMemoryManager` before reuse.

## Boundary validation

`CRNTIMemoryAdapter` accepts a non-zero 16-bit integer as the scheduler-supplied
key and rejects duplicates within one PUSCH batch. Semantic RNTI-type validity
and any reserved-value policy remain the responsibility of the gNB/RRC layer;
the memory subsystem does not duplicate those rules.

## Current source-tree limitation

The current `atlas-gpu01` workspace contains this orchestration repository,
`neural_rx`, and `srsran_open5gs` configuration/container material, but it does
not contain a checked-out srsRAN C++ source tree. Therefore the receiver-side
C-RNTI bridge and its lifecycle tests are implemented, but the final C++ call
site that extracts the C-RNTI from the concrete srsRAN PUSCH object cannot be
patched or compiled in this workspace yet.

When a concrete srsRAN source checkout is added, the remaining patch is only
boundary plumbing: obtain the already-known C-RNTIs in current receiver order,
call `lookup`, pass memory/gap/valid to the temporal NRX runtime, and commit the
returned memory under the immutable lookup keys.

Do not create a second UE-identity inference mechanism inside the neural model.
