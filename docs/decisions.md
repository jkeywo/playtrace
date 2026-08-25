# Decision register

This register records the product decisions adopted for M0. The attached system
design and Crisis Wardens files are inputs; only decisions recorded here are
treated as Play Trace project commitments.

| ID | Decision | Status | Consequence |
|---|---|---|---|
| `M0-D001` | Build one reasoning kernel around a derived `DesignSnapshot`, with project-specific adapters. | accepted | Medium-specific source handling stays outside the shared reasoning model. |
| `M0-D002` | Give every game its own Git repository; keep canonical game material in its native source and Play Trace state derived. | accepted | The shared repository contains no game-specific working state; local deletion cannot destroy game truth, and exports cannot silently become editable canon. |
| `M0-D003` | Preserve source-authored uncertainty as a first-class field independent of extraction confidence and approval. | accepted | Working and placeholder material can be reasoned about without being laundered into fact. |
| `M0-D004` | Link evidence to material claims using `supports`, `contradicts`, `context`, or `judgment`. | accepted | Reports must expose how evidence bears on conclusions rather than append decorative bibliographies. |
| `M0-D005` | Ordinary requests default to transactional persistence. | accepted | A requested experiment or change does not silently create durable design policy. |
| `M0-D006` | Separate analysis, design-change approval, and implementation approval. | accepted | A finding need not cause a mutation, and accepted design intent does not excuse an incorrect implementation. |
| `M0-D007` | Use Crisis Wardens as the first tabletop pilot from its own repository. | accepted | The first integration work is driven by a real incomplete game without checking its fixture into the shared Play Trace repository. |
| `M0-D008` | Treat Drive file IDs plus observed revisions as source identity; use separate stable IDs for cross-source design entities. | accepted | Provider identity and domain identity can evolve independently without losing provenance. |
| `M0-D009` | Hand-curate the M0 Crisis Wardens reference snapshot in the Crisis Wardens repository. | accepted | M0 can validate the cross-repository information contract before building arbitrary prose extraction or Drive synchronization. |
| `M0-D010` | Implement M0 as a shared-kernel skeleton using only a synthetic cross-medium fixture; make the first real-game integration read-only. | accepted | M0 proves source-neutral analysis now; Crisis Wardens remains external and is integrated without premature CNL, simulation, critics, or canonical writes. |
| `M0-D011` | Permit only explicitly reviewed generalisations to move from a game repository into Play Trace shared policy, cases, schemas, or plugins. | accepted | Game-specific learning cannot leak into shared behavior merely because a pilot was analysed. |

## Decision rule

Changing one of these decisions requires an explicit amendment with rationale.
Conversation, generated code, or a new source document does not silently update
the register.
